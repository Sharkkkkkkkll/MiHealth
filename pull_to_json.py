#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立脚本：调用米桥（Mi Fitness Data Bridge）API 拉取小米手环数据并导出为 JSON。

流程：登录小米云 -> 用 SyncService 把数据同步进本地 SQLite -> 用 export_database 导出 JSON。
全程数据留在你电脑上，不进任何第三方云。

运行环境：需在已安装 mi_fitness_mcp + mijiaAPI 的 Python 里跑（推荐你的系统 Python 3.14）：
    C:/Users/Administrator/AppData/Local/Programs/Python/Python314/python.exe pull_to_json.py

凭证获取（按优先级，扫码登录最高优先）：
    0) --qr-login：调 mijia-api 弹出二维码，用「米家 APP」扫码 -> 自动拿 fresh 账号级
       userId+passToken 写进系统钥匙串 -> 随后拉数据。这是最稳的一条命令搞定方案。
    1) 命令行传 --user-id / --pass-token（明文，仅在安全终端用）；
    2) 环境变量 MI_FITNESS_USER_ID / MI_FITNESS_PASS_TOKEN；
    3) 系统钥匙串中 `mi-fitness-bridge setup` 或本脚本 --qr-login 保存的 token。

为什么要用 --qr-login：
    小米的 passToken 是短命令牌，每次登录都会轮换，过期/登出后即失效。手动填的
    userId/passToken 很容易是失效值，于是桥接器报
    "Xiaomi login response is missing required fields (passToken, userId, ssecurity)"。
    用 mijia-api 扫码登录拿到的账号级凭据是新鲜有效的，桥接器会用它去换
    sid=miothealth（小米运动健康）的会话，从而正常拉数据。

用法示例：
    python pull_to_json.py --qr-login                 # 首次：弹二维码->扫码->全量播种最近90天；之后增量追拉，导出恒为最近90天滚动窗口
    python pull_to_json.py                             # 复用钥匙串里已有的 token（JSON 默认存 D:\\MIwatchAI\\JSON\\）
    python pull_to_json.py --days 30 --out d:/tmp/week.json   # --out 可覆盖默认路径
    python pull_to_json.py --start 2026-01-01 --end 2026-03-31
    python pull_to_json.py --types sleep,heart_rate --with-workout-detail
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from mi_fitness_mcp.adapters.mi_fitness_cloud import MiFitnessCloudAdapter
from mi_fitness_mcp.auth import load_mi_fitness_token, save_mi_fitness_token
from mi_fitness_mcp.config import ENV_DB_PATH
from mi_fitness_mcp.export import export_database
from mi_fitness_mcp.services.sync_service import SyncService
from mi_fitness_mcp.storage import Database

# 与 SyncService._SYNC_TARGETS 保持一致（不含 workout_detail，后者依赖 workouts 先落库）
ALL_TYPES = [
    "daily_activity",
    "sleep",
    "workouts",
    "body_measurements",
    "heart_rate",
    "spo2",
    "stress",
    "abnormal_heart_beat",
]


# 导出产物固定根目录（用户指定）
EXPORT_DIR = r"D:\MIwatchAI\JSON"                 # JSON 导出固定目录
DEFAULT_DB = r"D:\MIwatchAI\mi_fitness_local.db"  # 本地增量库固定位置（单用户）

# --------------------------------------------------------------------------- #
# 多账号钥匙串（方案 A，2026-10-15）：按 user_id 存多个 token。
# mi_fitness_mcp.auth 只支持单账号（固定账号名），这里用 keyring 直接实现
# 按用户隔离：账号名 mi_fitness_auth_<user_id>_user_id / _pass_token。
# 另存一个"活跃账号"指针（mi_fitness_auth_active），无 --user-id 时默认用它。
# --------------------------------------------------------------------------- #
KEYRING_SERVICE = "mi-fitness-mcp"
_ACTIVE_ACCT = "mi_fitness_auth_active"


def _keyring_account(user_id: str) -> str:
    """钥匙串账号名前缀：按 user_id 隔离；空 → 旧默认账号名（兼容单用户）。"""
    uid = (user_id or "").strip()
    if not uid:
        return "mi_fitness_auth"
    return f"mi_fitness_auth_{uid}"


def save_token_for(user_id: str, pass_token: str) -> None:
    """把 (user_id, pass_token) 按 user_id 写入钥匙串，并设为活跃账号。"""
    import keyring
    acct = _keyring_account(user_id)
    keyring.set_password(KEYRING_SERVICE, f"{acct}_user_id", user_id)
    keyring.set_password(KEYRING_SERVICE, f"{acct}_pass_token", pass_token)
    keyring.set_password(KEYRING_SERVICE, _ACTIVE_ACCT, user_id)


def load_token_for(user_id: str = "") -> tuple[str | None, str | None]:
    """按 user_id 从钥匙串取 token；user_id 空 → 活跃账号，再退回旧单账号。"""
    import keyring
    if user_id:
        acct = _keyring_account(user_id)
        uid = keyring.get_password(KEYRING_SERVICE, f"{acct}_user_id")
        tok = keyring.get_password(KEYRING_SERVICE, f"{acct}_pass_token")
        return (uid, tok) if uid and tok else (None, None)
    active = keyring.get_password(KEYRING_SERVICE, _ACTIVE_ACCT)
    if active:
        acct = _keyring_account(active)
        uid = keyring.get_password(KEYRING_SERVICE, f"{acct}_user_id")
        tok = keyring.get_password(KEYRING_SERVICE, f"{acct}_pass_token")
        if uid and tok:
            return uid, tok
    # 旧单账号（mi-fitness-bridge setup 写入的）
    try:
        uid, tok = load_mi_fitness_token()
        if uid and tok:
            return uid, tok
    except Exception:
        pass
    return None, None


def db_path_for(user_id: str = "") -> str:
    """本地 SQLite 按用户隔离：多账号各用独立库，避免增量水位互相干扰。"""
    uid = (user_id or "").strip()
    if not uid:
        return DEFAULT_DB
    return os.path.join(os.path.dirname(DEFAULT_DB), f"mi_fitness_local_{uid}.db")


def export_dir_for(user_id: str = "") -> str:
    """导出 JSON 目录按用户隔离：JSON/<user_id>/；空 → 根 JSON（兼容旧数据）。"""
    uid = (user_id or "").strip()
    if not uid:
        return EXPORT_DIR
    return os.path.join(EXPORT_DIR, uid)


# --------------------------------------------------------------------------- #
# 扫码登录（--qr-login）：用 mijia-api 拿新鲜账号级凭据，写进系统钥匙串
# --------------------------------------------------------------------------- #
def _clear_proxy_env() -> None:
    """mijia-api 用 requests，默认会读 HTTP(S)_PROXY。你的机器上是
    socks5://127.0.0.1:7897，requests 未装 PySocks 时走不了 SOCKS。而小米账号服务
    可直连（桥接器即用 trust_env=False 直连并成功拿到过响应），故清掉代理环境变量
    强制走直连，避免扫码请求被 SOCKS 代理卡死。"""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)


def qr_login_flow(user_id_hint: str = "") -> tuple[str, str]:
    """调 mijia-api 做二维码登录，返回 (user_id, pass_token) 并写入钥匙串。

    终端会打印 ASCII 二维码 + 一个可打开看图片的链接；用「米家 APP」扫这个码，
    脚本长轮询等待扫码完成。若之前扫码的 token 仍在有效期，会直接刷新返回、无需重扫。
    多账号：扫码拿到的 userId 即该用户的隔离 ID，token 按 userId 存钥匙串。
    """
    try:
        from mijiaAPI import mijiaAPI
    except ImportError:
        print(
            "缺少依赖 mijiaAPI。请先安装：\n"
            "  C:/Users/Administrator/AppData/Local/Programs/Python/Python314/python.exe "
            "-m pip install mijiaAPI\n"
            "（mijia-api 是 GPL-3.0 开源库，仅借用其扫码登录拿账号级凭据，"
            "凭据跨服务通用，可直接用于桥接器换取 Mi Fitness 会话）",
            file=sys.stderr,
        )
        raise SystemExit(2)

    print("[扫码登录] 正在准备二维码（会清掉 SOCKS 代理走直连）...")
    _clear_proxy_env()

    api = mijiaAPI()
    try:
        auth_data = api.login()  # 打印二维码 + 图片链接；长轮询等扫码；或刷新有效 token
    except Exception as exc:  # noqa: BLE001
        print(
            f"[扫码登录] 登录失败：{type(exc).__name__}: {exc}\n"
            "  若提示连接/超时，请确认本机能直连 account.xiaomi.com"
            "（或暂时关闭系统 SOCKS 代理后重试）。",
            file=sys.stderr,
        )
        raise SystemExit(1)

    user_id = str(auth_data.get("userId", ""))
    pass_token = str(auth_data.get("passToken", ""))
    if not user_id or not pass_token:
        print(
            f"[扫码登录] 返回数据缺少 userId/passToken：{list(auth_data.keys())}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    print(f"[扫码登录] 成功！userId={user_id[-4:].rjust(len(user_id),'*')} "
          f"（已掩码）passToken 长度={len(pass_token)}")
    print("[扫码登录] 正在把新鲜凭据写入系统钥匙串...")
    try:
        save_token_for(user_id, pass_token)
    except Exception as exc:  # noqa: BLE001
        print(f"[扫码登录] 写钥匙串失败：{type(exc).__name__}: {exc}\n"
              "  将仅本次内存使用，不影响拉数据。", file=sys.stderr)

    # 顺手把 auth.json 路径告诉用户，便于排查
    auth_json = Path.home() / ".config" / "mijia-api" / "auth.json"
    if auth_json.exists():
        print(f"[扫码登录] mijia-api 凭据已落盘：{auth_json}")
    return user_id, pass_token


def resolve_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """按 --qr-login > 命令行参数 > 环境变量 > 系统钥匙串 的优先级取凭证。

    多账号（方案 A）：
      · --qr-login：扫码登录，token 按扫码拿到的 userId 存钥匙串
      · --user-id 单独传（不带 --pass-token）：从钥匙串按该 user_id 取 token
      · --user-id + --pass-token：命令行明文
      · 都不传：取活跃账号 / 旧单账号
    """
    if getattr(args, "qr_login", False):
        print("[凭证] 走 --qr-login 扫码登录流程")
        return qr_login_flow()

    if args.user_id and args.pass_token:
        return args.user_id, args.pass_token

    if args.user_id:
        # 只传 --user-id：从钥匙串按该用户取 token（多账号）
        uid, tok = load_token_for(args.user_id)
        if uid and tok:
            print(f"[凭证] 使用钥匙串中 {args.user_id} 的 token")
            return uid, tok
        print(
            f"[凭证] 钥匙串中找不到 user_id={args.user_id} 的 token。\n"
            "  请先用 --qr-login 扫码登录该账号，或同时传 --pass-token。",
            file=sys.stderr,
        )
        raise SystemExit(2)

    env_uid = os.environ.get("MI_FITNESS_USER_ID")
    env_token = os.environ.get("MI_FITNESS_PASS_TOKEN")
    if env_uid and env_token:
        print("[凭证] 使用环境变量 MI_FITNESS_USER_ID / MI_FITNESS_PASS_TOKEN")
        return env_uid, env_token

    uid, tok = load_token_for("")   # 活跃账号 / 旧单账号
    if uid and tok:
        print("[凭证] 使用系统钥匙串中保存的 token（setup 或 --qr-login 写入）")
        return uid, tok

    print(
        "缺少小米账号凭证。推荐用扫码登录一次性解决：\n"
        "  python pull_to_json.py --qr-login\n"
        "或任选其一：\n"
        "  1) 运行 `mi-fitness-bridge setup` 登录小米账号（token 存钥匙串）\n"
        "  2) 设置环境变量 MI_FITNESS_USER_ID / MI_FITNESS_PASS_TOKEN\n"
        "  3) 命令行加 --user-id / --pass-token",
        file=sys.stderr,
    )
    raise SystemExit(2)


# --------------------------------------------------------------------------- #
# 本地库状态探测（决定是否走全量播种还是增量追拉；只读，不影响数据）
# --------------------------------------------------------------------------- #
def _open_ro(db_path: str):
    """以只读方式打开本地 SQLite（URI 模式，绝不写脏库）。"""
    return sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)


def db_is_seeded(db_path: str) -> bool:
    """本地库是否已做过首次播种（daily_activity 有记录即视为已播种）。"""
    if not os.path.exists(db_path):
        return False
    try:
        with _open_ro(db_path) as con:
            n = con.execute("SELECT COUNT(*) FROM daily_activity").fetchone()[0]
        return n > 0
    except Exception:
        return False


def _prune_old_jsons(keep: int, user_id: str = "") -> None:
    """仅清理用户导出目录内按时间命名的导出 JSON，保留最近 keep 个，避免无限膨胀。

    多账号：user_id 非空 → 只清理 JSON/<user_id>/ 内的快照，不碰其他用户的。
    """
    if keep <= 0:
        return
    d = export_dir_for(user_id)
    try:
        files = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".json")]
        files.sort(key=os.path.getmtime, reverse=True)
        for old in files[keep:]:
            try:
                os.remove(old)
                print(f"[清理] 删除旧快照 {os.path.basename(old)}（保留最近 {keep} 个）")
            except OSError as exc:
                print(f"[清理] 无法删除 {os.path.basename(old)}: {exc}", file=sys.stderr)
    except OSError:
        pass


def parse_types(value: str) -> list[str]:
    if value.strip().lower() == "all":
        return list(ALL_TYPES)
    chosen = [t.strip() for t in value.split(",") if t.strip()]
    unknown = [t for t in chosen if t not in ALL_TYPES and t != "workout_detail"]
    if unknown:
        print(f"未知数据类型: {unknown}\n可选: {ALL_TYPES} 或 workout_detail", file=sys.stderr)
        raise SystemExit(2)
    return chosen


async def _run(args: argparse.Namespace, user_id: str, pass_token: str,
               sync_start: str, sync_end: str,
               exp_start: str, exp_end: str,
               types: list[str], seed_mode: str) -> None:
    """全部异步操作放在同一个事件循环里完成。

    关键修复：旧版每次调用 asyncio.run() 都新建/销毁一个事件循环，而
    MiFitnessCloudAdapter 的 httpx.AsyncClient 被绑定到第一次 connect 的那个循环，
    后续类型在自己的新循环里复用已关闭的 client 会随机抛
    'Event loop is closed'，表现成「有的类型成功、有的失败」。
    现在 connect -> 同步所有类型 -> close 共用一个 loop，彻底消除该偶发失败。

    同步范围（sync_start~sync_end）与导出窗口（exp_start~exp_end）解耦：
    同步只负责把新数据增量写进本地 SQLite 主库；导出只切片最近 N 天，
    保证手机永远只看滚动 90 天，且导出文件不再随时间无限膨胀。
    """
    db_path = args.db or os.environ.get(ENV_DB_PATH) or db_path_for(user_id)
    db = Database(db_path)
    print(f"[数据库] {db_path}")

    adapter = MiFitnessCloudAdapter(user_id=user_id, pass_token=pass_token, region=args.region)
    print(f"[连接] 登录小米云（region={args.region}）...")
    if not await adapter.connect():
        print(f"登录失败：{adapter.last_error}", file=sys.stderr)
        print("提示：若之前手动填过凭据，多半是 token 失效。请用 --qr-login 重新扫码。",
              file=sys.stderr)
        raise SystemExit(1)
    print("[连接] 登录成功")

    svc = SyncService(adapter, db, default_lookback_days=args.days, chunk_days=7)

    print(f"[同步] 模式={seed_mode} {sync_start} ~ {sync_end}，类型：{types}")
    for t in types:
        try:
            result = await svc.sync_data_type(t, sync_start, sync_end, force_full=args.force_full)
            print(
                f"  - {t:20s} 新增 {result['added']:>5}  更新 {result['updated']:>5}  "
                f"跳过 {result['skipped']:>5}  [{result['status']}]"
            )
        except Exception as exc:  # 单类型失败不中断其余
            print(f"  - {t:20s} 同步出错: {type(exc).__name__}: {exc}", file=sys.stderr)

    await adapter.close()

    print(f"[导出] 滚动窗口 {exp_start} ~ {exp_end} -> {args.out}")
    written = export_database(db_path, args.out, output_format="json",
                              start_date=exp_start, end_date=exp_end)
    for path in written:
        counts = written.row_counts
        total = sum(counts.values())
        print(f"  已写出 {path}（共 {total} 条记录）")
        for name, n in counts.items():
            if n:
                print(f"    · {name}: {n}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="调用米桥 API 拉取小米手环数据并导出 JSON",
    )
    parser.add_argument("--db", default=None, help=f"本地 SQLite 路径（默认：{DEFAULT_DB}；也可设环境变量 MI_FITNESS_DB_PATH）")
    parser.add_argument("--out", default=None, help=f"导出 JSON 路径（默认：{EXPORT_DIR}\\<拉取时间>.json；传此参数可覆盖）")
    parser.add_argument("--start", help="开始日期 YYYY-MM-DD（默认：end - days + 1）")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD（默认：今天）")
    parser.add_argument("--days", type=int, default=90, help="滚动窗口/首次播种天数（默认 90）。导出 JSON 恒为最近 N 天，手机只看这 N 天")
    parser.add_argument("--overlap-days", type=int, default=7, help="增量追拉时回看的天数（默认 7）：每天补最近 N 天增量，容忍漏同步/云端延迟；本地库已播种全量历史，无需回看更早")
    parser.add_argument("--keep", type=int, default=3, help="保留最近 N 个导出快照，旧文件自动清理（默认 3；0=不清理）")
    parser.add_argument("--types", default="all", help="逗号分隔的数据类型，或 all（默认 all）")
    parser.add_argument("--region", default="cn", help="小米云区域（默认 cn）")
    parser.add_argument("--with-workout-detail", action="store_true", help="额外拉取运动秒级明细 + GPS 轨迹")
    parser.add_argument("--force-full", action="store_true", help="忽略增量水位，全量重拉")
    parser.add_argument("--user-id", help="小米 user_id（多账号隔离 ID；单独传=从钥匙串取该账号 token，或与 --pass-token 一起明文传）")
    parser.add_argument("--pass-token", help="小米 passToken（明文，仅在安全终端用）")
    parser.add_argument("--qr-login", action="store_true",
                        help="调 mijia-api 弹二维码，用米家 APP 扫码登录，自动拿新鲜 token 写钥匙串")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印调试日志")
    args = parser.parse_args()

    # 多账号（方案 A）：--user-id 决定数据库与导出目录的隔离位置。
    # 注意：--user-id 可能同时用于"取钥匙串 token"（resolve_credentials 里），
    # 这里在 resolve 之前先解析出隔离 ID。
    uid_arg = (args.user_id or "").strip()

    # 固定导出目录 + 按"拉取时间"命名；数据库按用户隔离，保证增量水位稳定
    if not args.db:
        args.db = db_path_for(uid_arg)
    if not args.out:
        out_dir = export_dir_for(uid_arg)
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        args.out = os.path.join(out_dir, f"{stamp}.json")
        print(f"[导出路径] {args.out}")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    user_id, pass_token = resolve_credentials(args)
    today = date.today()
    db_path = args.db

    # ---- 决定同步范围：首次全量播种 / 之后增量追拉 ----
    if args.force_full or not db_is_seeded(db_path):
        # 首次或强制：播种 --days 天（与 --start/--end 兼容）
        if args.start:
            sync_start = args.start
        else:
            sync_start = (today - timedelta(days=max(1, args.days) - 1)).isoformat()
        sync_end = args.end or today.isoformat()
        seed_mode = "全量播种"
    else:
        # 增量追拉：统一回看最近 --overlap-days 天（默认 7）。
        # 本地库已在首次播种时落全量历史，此处只需补最近的增量；
        # 多日重叠既兜底云端延迟到达，也容忍偶尔漏同步几天。
        # DB 幂等 upsert，重复区间不会写重复记录。
        sync_start = (today - timedelta(days=max(1, args.overlap_days))).isoformat()
        sync_end = today.isoformat()
        seed_mode = "增量追拉"

    # ---- 导出滚动窗口：始终最近 --days 天（手机只呈现前 N 天）----
    exp_end = today.isoformat()
    exp_start = (today - timedelta(days=max(1, args.days) - 1)).isoformat()

    types = parse_types(args.types)
    if args.with_workout_detail and "workout_detail" not in types:
        types.append("workout_detail")

    # 保证 workout_detail 排在 workouts 之后（明细依赖 workouts 元数据）
    if "workout_detail" in types:
        types.remove("workout_detail")
        types.append("workout_detail")

    asyncio.run(_run(args, user_id, pass_token, sync_start, sync_end,
                     exp_start, exp_end, types, seed_mode))

    # 清理旧快照：仅删除该用户导出目录内按时间命名的导出 JSON，保留最近 --keep 个
    _prune_old_jsons(args.keep, uid_arg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        raise SystemExit(130)
