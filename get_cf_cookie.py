"""
War Thunder cf_clearance 一键获取工具 (v3)
===========================================
从 Chrome 的 Cookie 数据库中直接读取 cf_clearance。
无需启动浏览器，无需 CDP，无需 WebSocket。

用法:
    1. 用 Chrome 正常访问 https://warthunder.com/en/community/userinfo/?nick=HOSO6
    2. 等页面加载完（能看到战绩数据）
    3. 关闭 Chrome
    4. 运行: python get_cf_cookie.py
"""
import os
import sqlite3
import shutil
import sys
import tempfile


def find_chrome_cookie_db():
    """查找 Chrome 的 Cookie 数据库文件。"""
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Network\Cookies"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Cookies"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\Profile 1\Network\Cookies"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Network\Cookies"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Cookies"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def main():
    print("=" * 60)
    print("  War Thunder cf_clearance 一键获取工具")
    print("=" * 60)
    print()

    cookie_db = find_chrome_cookie_db()
    if not cookie_db:
        print("  ✗ 未找到 Chrome/Edge 的 Cookie 数据库。")
        print()
        print("  请确保已安装 Chrome 或 Edge 浏览器。")
        input("按回车键退出...")
        return

    print(f"  找到 Cookie 数据库: {cookie_db}")
    print()

    # Chrome 锁定了 Cookie 数据库，需要复制一份再读取
    tmp_db = os.path.join(tempfile.gettempdir(), "wt_chrome_cookies.db")
    try:
        shutil.copy2(cookie_db, tmp_db)
    except Exception as e:
        print(f"  ✗ 无法复制 Cookie 文件: {e}")
        print("  请确保 Chrome 已完全关闭后再运行本脚本。")
        input("按回车键退出...")
        return

    try:
        conn = sqlite3.connect(tmp_db)
        cur = conn.cursor()

        # 查询 warthunder.com 的 cf_clearance
        cur.execute(
            "SELECT name, value, host_key FROM cookies "
            "WHERE host_key LIKE '%warthunder.com' AND name = 'cf_clearance'"
        )
        rows = cur.fetchall()

        if not rows:
            print("  ✗ 未找到 cf_clearance cookie。")
            print()
            print("  请按以下步骤操作：")
            print("    1. 打开 Chrome 浏览器")
            print("    2. 访问 https://warthunder.com/en/community/userinfo/?nick=HOSO6")
            print("    3. 等待页面完全加载（能看到战绩数据）")
            print("    4. 完全关闭 Chrome（所有窗口）")
            print("    5. 重新运行本脚本")
            print()
            conn.close()
            input("按回车键退出...")
            return

        cf_clearance = rows[0][1]
        host = rows[0][2]

        print("  ✓ 成功获取 cf_clearance！")
        print()
        print("=" * 60)
        print("  请将以下内容复制到 AstrBot 插件配置中：")
        print("=" * 60)
        print()
        print("  cf_clearance:")
        print(f"  {cf_clearance}")
        print()
        print("=" * 60)
        print("  配置说明:")
        print("   1. 打开 AstrBot 管理面板 → 插件管理 → 战雷战绩查询 → 配置")
        print("   2. 找到 cf_clearance 字段，粘贴上面的值")
        print("   3. browser_user_agent 留空即可（会自动使用默认值）")
        print("   4. 保存配置即可")
        print("   5. cookie 有效期一般几天，过期重新操作一次")
        print("   6. 配置后可查询任何玩家，不限制特定用户")
        print("=" * 60)

        try:
            import pyperclip
            pyperclip.copy(cf_clearance)
            print("\n  cf_clearance 已自动复制到剪贴板")
        except ImportError:
            pass

        print()
        conn.close()

    except Exception as e:
        print(f"  ✗ 读取失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            os.remove(tmp_db)
        except Exception:
            pass

    input("按回车键退出...")


if __name__ == "__main__":
    main()