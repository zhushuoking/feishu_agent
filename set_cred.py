"""交互式写入飞书应用凭证，并立即向飞书验证是否可用。

比手动编辑 .env 靠谱：会自动去掉粘贴时带进来的空格和引号，
写完立刻调一次 tenant_access_token 接口，凭证错了当场就能知道。
"""

import os

import httpx

ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def set_key(lines, key, value):
    out, done = [], False
    for line in lines:
        if line.strip().startswith(key + "="):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    return out


def main():
    print("=" * 52)
    print("  写入飞书应用凭证")
    print("=" * 52)

    if not os.path.exists(ENV):
        print("找不到 .env，请先运行 setup.bat")
        return

    with open(ENV, encoding="utf-8") as f:
        lines = f.read().splitlines()

    app_id = input("粘贴 App ID（cli_ 开头）: ").strip().strip('"').strip("'")
    app_secret = input("粘贴 App Secret: ").strip().strip('"').strip("'")

    if not app_id or not app_secret:
        print("\n[错误] 不能为空，已退出")
        return

    if not app_id.startswith("cli_"):
        print("\n[提醒] App ID 一般以 cli_ 开头，确认一下是不是复制错了")

    lines = set_key(lines, "FEISHU_APP_ID", app_id)
    lines = set_key(lines, "FEISHU_APP_SECRET", app_secret)
    with open(ENV, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n已写入: {ENV}")
    print(f"  App ID     长度 {len(app_id)}")
    print(f"  App Secret 长度 {len(app_secret)}")

    print("\n正在向飞书验证凭证 ...")
    try:
        resp = httpx.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=15,
        )
        data = resp.json()
    except Exception as exc:
        print(f"[网络异常] {exc}")
        print("如果公司网络有限制，先确认能打开 open.feishu.cn")
        return

    if data.get("code") == 0:
        print("\n[成功] 凭证有效，tenant_access_token 已获取")
        print("下一步：双击 run.bat 启动长连接")
    else:
        print(f"\n[失败] code={data.get('code')} msg={data.get('msg')}")
        print("常见原因：")
        print("  1. App Secret 抄漏了字符")
        print("  2. 复制时带了换行或空格")
        print("  3. 应用刚创建，等一两分钟再试")


if __name__ == "__main__":
    main()
