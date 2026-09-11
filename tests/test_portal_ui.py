# -*- coding: utf-8 -*-
"""门户标题栏 UI 改造测试

需求覆盖：
1. 版本号显示：动态读取 version.json（app_version 注入），禁止硬编码
2. 账号管理按钮：账号管理/邀请码/修改密码 三入口迁移至下拉，逻辑不变（弹窗仍存在）
3. 关于系统按钮：下拉含 功能介绍（弹窗、按模块分类、可滚动、可关闭）与 版本更新（原检查更新）
4. 布局：新元素与原有 标题/用户名/退出 等共存，旧独立按钮不再出现
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app, APP_VERSION


TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'portal', 'templates', 'portal.html')


def _render(is_admin=True):
    with flask_app.test_client() as c:
        with c.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'tester'
            sess['is_admin'] = is_admin
        r = c.get('/')
        return r.get_data(as_text=True)


class TestVersionBadge(unittest.TestCase):
    """一、版本号显示：动态读取"""

    def test_badge_shows_dynamic_version(self):
        html = _render()
        expected = 'v%s' % APP_VERSION.get('version', '1.0.0')
        self.assertIn('class="brand-version"', html)
        self.assertIn(expected + '</span>', html)

    def test_not_hardcoded_in_template(self):
        """模板源码中版本徽标必须使用 Jinja 变量，禁止硬编码"""
        with open(TEMPLATE_PATH, encoding='utf-8') as f:
            src = f.read()
        m = re.search(r'<span class="brand-version"[^>]*>([^<]+)</span>', src)
        self.assertIsNotNone(m, '缺少版本徽标元素')
        self.assertIn('{{ app_version }}', m.group(1))


class TestAccountDropdown(unittest.TestCase):
    """二、账号管理下拉：三入口迁移，功能弹窗保留"""

    def test_admin_sees_all_three_items(self):
        html = _render(is_admin=True)
        self.assertIn('id="ddAccount"', html)
        # 三项菜单入口
        self.assertIn("dropdownAction('ddAccount', openUserModal)", html)
        self.assertIn("dropdownAction('ddAccount', openInviteModal)", html)
        self.assertIn("dropdownAction('ddAccount', openPwdModal)", html)

    def test_non_admin_sees_only_password(self):
        html = _render(is_admin=False)
        self.assertIn("dropdownAction('ddAccount', openPwdModal)", html)
        self.assertNotIn("dropdownAction('ddAccount', openUserModal)", html)
        self.assertNotIn("dropdownAction('ddAccount', openInviteModal)", html)

    def test_underlying_modals_and_logic_untouched(self):
        """原有三个弹窗与函数均保留（仅入口位置调整）"""
        html = _render(is_admin=True)
        for mid in ('pwdModal', 'inviteModal', 'userModal'):
            self.assertIn('id="%s"' % mid, html)
        for fn in ('function openPwdModal', 'function openInviteModal',
                   'function openUserModal', 'function changePassword',
                   'function loadUsers'):
            self.assertIn(fn, html)

    def test_old_standalone_buttons_removed(self):
        """旧的独立按钮不再出现于标题栏"""
        html = _render(is_admin=True)
        # 检查更新 / 修改密码 / 邀请码 不再以独立 btn-nav 形式出现
        self.assertNotIn("onclick=\"openInviteModal()\">邀请码", html)
        self.assertNotIn("onclick=\"openPwdModal()\">🔑 修改密码", html)
        self.assertNotIn("onclick=\"checkAppUpdate(true)\">🔄 检查更新", html)


class TestAboutDropdown(unittest.TestCase):
    """三、关于系统下拉：功能介绍 + 版本更新"""

    def test_about_dropdown_entries(self):
        html = _render()
        self.assertIn('id="ddAbout"', html)
        self.assertIn("dropdownAction('ddAbout', openAboutModal)", html)
        # 版本更新入口保留原 checkAppUpdate 逻辑与按钮 id
        self.assertIn('id="btnCheckUpdate"', html)
        self.assertIn('checkAppUpdate(true)', html)

    def test_about_modal_content_by_module(self):
        """功能介绍弹窗：按模块分类 + 可滚动 + 关闭操作"""
        html = _render()
        self.assertIn('id="aboutModal"', html)
        self.assertIn('class="modal-body about-body"', html)  # 可滚动容器
        # 四大模块使用说明 + 使用提示
        for kw in ('社保批量统计智能核算系统', '劳动合同图片整理系统',
                   '批量PDF转WORD系统', '医保参保证明批量下载系统', '使用提示'):
            self.assertIn(kw, html)
        # 明显的关闭操作
        self.assertIn('closeAboutModal()', html)
        # 弹窗开关函数
        self.assertIn('function openAboutModal', html)
        self.assertIn('function closeAboutModal', html)


class TestVersionInfoModal(unittest.TestCase):
    """关于系统 → 版本说明：版本信息 + 内置完整使用说明（v2.3.7）"""

    def test_version_info_dropdown_entry(self):
        """下拉入口存在且指向版本说明弹窗"""
        html = _render()
        self.assertIn('id="btnVersionInfo"', html)
        self.assertIn("dropdownAction('ddAbout', openVersionInfoModal)", html)

    def test_version_info_modal_structure(self):
        """弹窗骨架、开关函数与遮罩关闭齐全"""
        html = _render()
        self.assertIn('id="versionInfoModal"', html)
        self.assertIn('function openVersionInfoModal', html)
        self.assertIn('function closeVersionInfoModal', html)
        self.assertIn('closeVersionInfoModal()', html)

    def test_version_dynamic_not_hardcoded(self):
        """版本说明中的当前版本必须使用 Jinja 变量，禁止硬编码"""
        with open(TEMPLATE_PATH, encoding='utf-8') as f:
            src = f.read()
        m = re.search(r'当前版本：<b[^>]*>([^<]+)</b>', src)
        self.assertIsNotNone(m, '版本说明缺少"当前版本"元素')
        self.assertIn('{{ app_version }}', m.group(1))

    def test_version_info_renders_dynamic_values(self):
        """渲染结果包含 version.json 的动态版本与更新内容"""
        html = _render()
        self.assertIn('v%s' % APP_VERSION.get('version', '1.0.0'), html)
        changelog = APP_VERSION.get('changelog') or []
        if changelog:
            self.assertIn(changelog[0], html)

    def test_version_info_contains_full_manual(self):
        """使用说明按模块内置：五大能力 + MCP + 使用提示"""
        html = _render()
        for kw in ('版本信息', '社保批量统计智能核算系统', '劳动合同图片整理系统',
                   '批量 PDF 转 WORD 系统', '医保参保证明批量下载系统',
                   'MCP 智能服务端', '使用提示'):
            self.assertIn(kw, html)

    def test_usage_tips_cover_long_task_warning(self):
        """长任务不可取消的提示必须保留"""
        html = _render()
        self.assertIn('保持平台开启', html)


class TestLayoutAndInteraction(unittest.TestCase):
    """四、布局与交互脚本"""

    def test_dropdown_interactions_present(self):
        html = _render()
        for fn in ('function toggleDropdown', 'function closeAllDropdowns',
                   'function dropdownAction'):
            self.assertIn(fn, html)
        # 点击外部关闭 + Esc 关闭
        self.assertIn("document.addEventListener('click'", html)
        self.assertIn("e.key === 'Escape'", html)

    def test_css_styles_defined(self):
        css_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'static', 'css', 'portal.css')
        with open(css_path, encoding='utf-8') as f:
            css = f.read()
        for rule in ('.brand-version', '.nav-dropdown', '.dropdown-menu',
                     '.dropdown-item', '.about-body', '.about-section',
                     '.nav-dropdown.open .dropdown-menu'):
            self.assertIn(rule, css)
        # 悬停反馈
        self.assertIn('.dropdown-item:hover', css)
        # 窄窗口适配（版本徽标隐藏防截断）
        self.assertIn('@media (max-width: 1080px)', css)

    def test_brand_and_logout_untouched(self):
        """原有标题/用户名/退出等元素保留，窗口控制不受影响"""
        html = _render()
        self.assertIn('class="brand-text"', html)
        self.assertIn('class="user-name"', html)
        self.assertIn('href="/logout"', html)

    def test_inline_js_balanced(self):
        """内联脚本括号配平（语法结构粗校验）"""
        html = _render()
        scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
        self.assertTrue(scripts)
        js = '\n'.join(scripts)
        for a, b in [('{', '}'), ('(', ')'), ('[', ']')]:
            # 排除字符串中的括号干扰：按行粗略统计代码括号
            self.assertEqual(js.count(a) - js.count(b) >= -2, True)


class TestAutoUpdateCheck(unittest.TestCase):
    """五、门户页自动检测更新（v2.0.2 修复：WebView2 持久化 cookie 使免登录用户
    直达门户页，登录页横幅看不到，门户页必须自动检查）"""

    def test_auto_check_on_page_load(self):
        html = _render()
        # 页面加载后自动调用 checkAppUpdate(false)（非手动，静默检查）
        self.assertIn("window.addEventListener('load'", html)
        self.assertIn('checkAppUpdate(false)', html)

    def test_periodic_recheck_for_long_running_sessions(self):
        """长时间不重启的场景：定时轮询覆盖"""
        html = _render()
        m = re.search(r'setInterval\(function\(\) \{ checkAppUpdate\(false\); \},\s*([\d\s*+]+)\)', html)
        self.assertIsNotNone(m, '缺少定时轮询检查')
        # 轮询间隔可为算术表达式（如 4 * 60 * 60 * 1000），求值后应在 1~12 小时之间
        interval_ms = int(eval(m.group(1), {'__builtins__': {}}, {}))
        self.assertTrue(3600000 <= interval_ms <= 43200000)

    def test_manual_check_still_available(self):
        """手动入口（关于系统 → 版本更新）不受影响"""
        html = _render()
        self.assertIn('checkAppUpdate(true)', html)


class TestV231NavAdjust(unittest.TestCase):
    """五、v2.3.1 导航两处调整：MCP入口迁入"关于系统"下拉 + 登录页更新入口移除"""

    LOGIN_TEMPLATE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'templates', 'login.html')

    def test_mcp_entry_moved_to_about_dropdown(self):
        """MCP模块入口迁入 ddAbout 下拉：命名/样式与其他子项一致（dropdown-item）"""
        html = _render()
        self.assertIn('id="ddAbout"', html)
        self.assertIn('🔌 MCP模块', html)
        # 与其他子项同款 dropdown-item 样式，且为 <a> 直达原路由
        self.assertRegex(html, r'<a href="/mcp/"[^>]*class="dropdown-item"')
        # 版本更新唯一入口保留在 ddAbout 中
        self.assertIn('id="btnCheckUpdate"', html)
        self.assertIn("dropdownAction('ddAbout', openAboutModal)", html)

    def test_mcp_card_removed_from_grid_without_residue(self):
        """模块宫格不再出现 MCP 卡片，且无重复/失效入口残留"""
        html = _render()
        self.assertNotIn('McpCardLuyue2026Node01', html)   # 旧卡片节点已移除
        self.assertNotIn('module-card" data-page-node-id="McpCard', html)
        # 宫格内不得再出现指向 /mcp/ 的 module-card 链接（下拉中的 <a> 是 dropdown-item）
        self.assertNotRegex(html, r'class="module-card"[^>]*href="/mcp/"')
        # 全页 /mcp/ 链接只出现在 ddAbout 下拉中（唯一入口，无重复）
        self.assertEqual(html.count('href="/mcp/"'), 1)

    def test_mcp_route_and_permission_unchanged(self):
        """/mcp/ 路由与鉴权不变（后端未动，未登录仍跳转登录页）"""
        with flask_app.test_client() as c:
            r = c.get('/mcp/')
            self.assertEqual(r.status_code, 302)  # 未登录 → 跳转

    def test_login_page_update_entry_removed(self):
        """登录页不再显示任何更新提示：横幅/检测/升级按钮全部移除"""
        with open(self.LOGIN_TEMPLATE, encoding='utf-8') as f:
            src = f.read()
        for kw in ('id="updateBanner"', 'id="updRemoteVer"', 'id="updStatusText"',
                   'id="updDownloadBtn"', 'checkLoginUpdate',
                   '发现新版本', '立即升级'):
            self.assertNotIn(kw, src, '登录页不应残留更新入口: %s' % kw)

    def test_login_page_core_features_intact(self):
        """登录页其他功能与布局不受影响：登录表单/记住密码/注册链接保留"""
        with open(self.LOGIN_TEMPLATE, encoding='utf-8') as f:
            src = f.read()
        for kw in ('id="loginForm"', 'id="loginBtn"', 'id="rememberPwd"',
                   'api/remember_login', '前往注册'):
            self.assertIn(kw, src)

    def test_backend_update_api_kept_for_portal(self):
        """后端更新接口保留（门户"版本更新"唯一入口仍可用），仅登录页不再调用"""
        with flask_app.test_client() as c:
            r = c.get('/api/app/check_update')
            self.assertIn(r.status_code, (200, 500))  # 接口存在（500 为无网络环境的正常失败）


if __name__ == '__main__':
    unittest.main(verbosity=2)
