"""启动器内联 HTML 的体积守卫。

WebView2 的 NavigateToString 对 HTML 总大小有限制（约 2MB）；
启动器把图片 base64 内联进 HTML，大图会直接让窗口初始化失败
（"值不在预期的范围内"）。曾因此把 1.7MB 的庭院原图内联进去导致白屏。
"""

from launcher.app import _launcher_html

# WebView2 NavigateToString 的实际上限；留一倍余量防止临界踩线。
NAVIGATE_TO_STRING_BUDGET = 1_000_000


def test_launcher_html_stays_under_webview2_limit():
    html = _launcher_html()
    assert len(html) < NAVIGATE_TO_STRING_BUDGET, (
        f"启动器 HTML 已膨胀到 {len(html)} 字符，"
        "WebView2 NavigateToString 会拒绝渲染，请把新增图片先缩成小图再内联"
    )
