"""M4 隔离的真实端到端自测：拿两个账号互相"打"一遍。

它和 tests/test_isolation.py 的分工：
- pytest 那套测的是**函数和规则**（跑在进程内，快、每次都跑）
- 这个脚本测的是**部署后的真实 HTTP 行为**（带 uvicorn、带数据库、带真实 token），
  用来回答"我改完之后，线上这个口子真的关上了吗"

为什么要有它：
隔离这种东西失效时**不会有任何报错**——接口照样 200，只是多给了别人的数据。
没有主动去打自己，就无法证明"锁上了"。这是安全测试和功能测试最大的区别。

运行（后端需先启动）：
    python scripts/check_isolation.py
    python scripts/check_isolation.py --base http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import os
import random
import string
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# 密码固定，邮箱随机：每次跑都是全新账号，不依赖库里已有数据
_PASSWORD = "isolation-check-123"


def _rand_email(tag: str) -> str:
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"iso-{tag}-{suffix}@webpilot.dev"


class Result:
    """收集结果：最后一起打印，避免中途失败看不到全貌。"""

    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, name: str, detail: str = "") -> None:
        self.rows.append((ok, name, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f"  —— {detail}" if detail else ""))

    def summary(self) -> int:
        bad = [r for r in self.rows if not r[0]]
        print()
        print(f"共 {len(self.rows)} 项，通过 {len(self.rows) - len(bad)}，失败 {len(bad)}")
        if bad:
            print("失败项：")
            for _, name, detail in bad:
                print(f"  · {name}  {detail}")
            return 1
        return 0


def _register(client: httpx.Client, tag: str) -> tuple[str, str, int]:
    """注册一个账号，返回 (email, token, user_id)。"""
    email = _rand_email(tag)
    r = client.post("/api/v1/auth/register", json={"email": email, "password": _PASSWORD})
    r.raise_for_status()
    data = r.json()
    return email, data["token"], data["user_id"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("WEBPILOT_BASE", "http://127.0.0.1:8000"),
                help="后端地址；容器里跑传 http://api:8000（worker 的 127.0.0.1 不是 api）")
    args = ap.parse_args()

    # trust_env=False：本机开着系统代理时，httpx 会把 localhost 请求也丢给代理，
    # 拿到 "upstream connect failed" 这种假故障（本次开发就踩了）
    client = httpx.Client(base_url=args.base, timeout=30.0, trust_env=False)
    res = Result()

    print(f"目标后端：{args.base}")
    print("准备两个账号 A / B ...")
    _, token_a, uid_a = _register(client, "a")
    _, token_b, uid_b = _register(client, "b")
    ha = {"Authorization": f"Bearer {token_a}"}
    hb = {"Authorization": f"Bearer {token_b}"}
    print(f"  A = user_id {uid_a} / B = user_id {uid_b}\n")

    # ---------------------------------------------------------------- 任务隔离
    print("[1] 任务归属隔离")
    r = client.post(
        "/api/v1/tasks",
        json={"goal": "M4 隔离自测任务（可删）", "start_url": "http://127.0.0.1:8000/testsite/index.html"},
        headers=ha,
    )
    res.check(r.status_code in (200, 202), "A 提交任务成功", f"HTTP {r.status_code}")
    task_id = r.json().get("task_id", "")
    res.check(bool(task_id), "拿到 task_id", task_id)

    r = client.get(f"/api/v1/tasks/{task_id}")
    res.check(r.status_code == 401, "无 token 查任务 → 401", f"实际 HTTP {r.status_code}")

    r = client.get(f"/api/v1/tasks/{task_id}", headers=hb)
    res.check(r.status_code == 404, "B 查 A 的任务 → 404（不是 403）", f"实际 HTTP {r.status_code}")

    r = client.get(f"/api/v1/tasks/{task_id}/trace", headers=hb)
    res.check(r.status_code == 404, "B 查 A 的轨迹 → 404", f"实际 HTTP {r.status_code}")

    r = client.get(f"/api/v1/tasks/{task_id}", headers=ha)
    res.check(r.status_code == 200, "A 查自己的任务 → 200", f"实际 HTTP {r.status_code}")

    # ---------------------------------------------------------------- 列表隔离
    print("\n[2] 任务列表隔离")
    r = client.get("/api/v1/tasks", headers=ha)
    items_a = r.json().get("items", []) if r.status_code == 200 else []
    res.check(
        any(i["task_id"] == task_id for i in items_a),
        "A 的列表里能看到自己的任务",
        f"共 {len(items_a)} 条",
    )

    r = client.get("/api/v1/tasks", headers=hb)
    items_b = r.json().get("items", []) if r.status_code == 200 else []
    res.check(
        all(i["task_id"] != task_id for i in items_b),
        "B 的列表里看不到 A 的任务",
        f"共 {len(items_b)} 条",
    )

    r = client.get("/api/v1/tasks")
    res.check(r.status_code == 401, "无 token 列任务 → 401", f"实际 HTTP {r.status_code}")

    # ---------------------------------------------------------------- 媒体签名
    print("\n[3] 任务媒体（截图/语音）签名校验")
    # 找一个真的存在截图的目录来测（不依赖上面那个刚提交、还没跑的任务）
    from app.storage import files  # noqa: E402  （延迟导入：确保 sys.path 已就绪）

    runs = ROOT / "runs"
    sample_task, sample_png = "", ""
    for d in sorted(runs.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        pngs = sorted(d.glob("step_*.png"))
        if pngs:
            sample_task, sample_png = d.name, pngs[0].name
            break
    if not sample_task:
        res.check(False, "找到可测的截图样本", "runs/ 下没有 step_*.png，先跑一个任务")
    else:
        plain = f"/runs/{sample_task}/{sample_png}"
        r = client.get(plain)
        res.check(
            r.status_code == 403,
            "无签名访问截图 → 403（修复前是 200，就是那个漏洞）",
            f"实际 HTTP {r.status_code}",
        )

        r = client.get(f"{plain}?e=9999999999&sig={'0' * 32}")
        res.check(r.status_code == 403, "伪造签名 → 403", f"实际 HTTP {r.status_code}")

        # 正例：用项目自己的签名函数生成地址（等价于 get_trace 接口下发的地址）
        signed = files.screenshot_url(sample_task, sample_png)
        r = client.get(signed)
        res.check(r.status_code == 200, "合法签名 → 200", f"实际 HTTP {r.status_code}")

        # 把签名移植到别的任务上必须失效
        other = f"/runs/{task_id}/{sample_png}"
        query = signed.split("?", 1)[1]
        r = client.get(f"{other}?{query}")
        res.check(
            r.status_code == 403,
            "把签名移植到别的 task_id → 403",
            f"实际 HTTP {r.status_code}",
        )

        # meta.json / trace.json 即使签名正确也不给（任务全文只能走鉴权接口）
        for name in ("meta.json", "trace.json"):
            target = runs / sample_task / name
            if not target.exists():
                continue
            signed_json = files.signing.sign_url(sample_task, name)
            r = client.get(signed_json)
            res.check(
                r.status_code == 403,
                f"{name} 带签名也拒绝 → 403",
                f"实际 HTTP {r.status_code}",
            )

    # ---------------------------------------------------------------- 语音入口
    print("\n[4] 语音转写要登录（原接口无认证，可白嫖 ASR 算力）")
    r = client.post("/api/v1/voice/transcribe", files={"file": ("a.webm", b"x", "audio/webm")})
    res.check(
        r.status_code in (401, 503),
        "无 token 转写 → 401（VOICE_ENABLED=false 时 503）",
        f"实际 HTTP {r.status_code}",
    )

    code = res.summary()
    if code == 0:
        print("\nM4 隔离验收通过。测试账号与任务留着无妨（goal 里带「可删」标记）。")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
