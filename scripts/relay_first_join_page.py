"""Browser interactions for the isolated two-host relay acceptance run."""
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


class FirstJoinPage:
    def __init__(self, stack, base_url, evidence):
        self.evidence = evidence
        self.errors = []
        pw = stack.enter_context(sync_playwright())
        browser = pw.chromium.launch(channel='chrome', headless=True)
        stack.callback(browser.close)
        self.page = browser.new_page(viewport={'width': 1280, 'height': 900})
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto(base_url.rstrip('/') + '/app/', wait_until='domcontentloaded')
        self.click('btn-settings')
        self.click('btn-relay-nodes')
        expect(self.el('relay-add')).to_be_enabled()
        expect(self.el('relay-node-list')).to_contain_text('暂无远程')
        self.shot('empty')

    def el(self, element):
        return self.page.locator('#' + element)

    def click(self, element):
        self.el(element).click(timeout=10000)

    def shot(self, name):
        self.page.screenshot(path=self.evidence / (name + '.png'),
                             mask=[self.page.locator(selector + ':visible') for selector in
                                   ('#relay-join-command', '#relay-join-url', '#relay-pairing-url',
                                    '#relay-issued-credential')])

    def post(self, path, action):
        with self.page.expect_response(lambda r: urlsplit(r.url).path == path and r.request.method == 'POST') as result:
            action()
        return result.value

    def preview(self, link):
        self.click('relay-add')
        self.click('relay-join-dial')
        field = self.el('relay-join-url')
        field.fill('invalid')
        submit = self.el('relay-join-form').locator('button').click
        assert self.post('/relay/pairings/preview', submit).status == 400
        expect(self.el('relay-join-status')).to_have_attribute('data-error', 'true')
        self.shot('invalid-link')
        fingerprint = parse_qs(urlsplit(link).query)['fingerprint'][0]
        wrong = ('0' if fingerprint[0] != '0' else '1') + fingerprint[1:]
        field.fill(link.replace(fingerprint, wrong))
        assert self.post('/relay/pairings/preview', submit).status == 502
        expect(self.el('relay-join-status')).to_contain_text('证书核验失败')
        self.shot('certificate-failure')
        field.fill(link)
        first = self.post('/relay/pairings/preview', submit).json()
        expect(self.el('relay-join-identity')).to_contain_text(first['member'])
        self.click('relay-join-cancel')
        expect(self.el('relay-join-dialog')).not_to_be_visible()
        expect(self.el('relay-node-list')).to_contain_text('暂无远程')
        self.click('relay-add')
        self.click('relay-join-dial')
        field.fill(link)
        view = self.post('/relay/pairings/preview', submit).json()
        expect(self.el('relay-join-identity')).to_contain_text(view['member'])
        self.shot('verified-outbound')
        return view

    def confirm(self, path, member):
        expect(self.el('relay-join-identity')).to_contain_text(member, timeout=15000)
        view = self.post(path + '/confirm', lambda: self.click('relay-join-confirm')).json()
        route = view['node_member']
        expect(self.page.locator(f'article[data-relay-member="{route}"]')).to_be_visible()
        return view

    def invite(self, lan):
        self.click('relay-add')
        self.click('relay-join-listen')
        self.click('relay-listen-close')
        expect(self.el('relay-join-dialog')).to_be_visible()
        self.click('relay-join-listen')
        self.el('relay-listen-address').select_option(lan)
        self.shot('listen-settings')
        issued = self.post('/relay/invitations', lambda: self.click('relay-listen-save')).json()
        expect(self.el('relay-join-invite')).to_be_visible()
        command = self.el('relay-join-command').input_value()
        assert issued['invite_token'] in command and issued['engine_url'] in command
        return issued

    def cards_online(self, *members, capture=False):
        self.click('relay-refresh')
        seen = []
        for member in members:
            card = self.page.locator(f'article[data-relay-member="{member}"]')
            expect(card.locator('.relay-badge').first).to_have_text('在线')
            seen.append(card.locator('.relay-node-name').inner_text())
            if capture:
                card.locator('.relay-node-head').screenshot(path=self.evidence / f'{member}-label.png')
        assert set(seen) == {'default（远程）', 'default（远程 2）'}
        self.shot('online')
        return seen

    def switch(self, member, link):
        self.page.locator(f'article[data-relay-member="{member}"] button[data-action="direction"]').click()
        self.click('relay-direction-dial')
        self.el('relay-pairing-url').fill(link)
        self.click('relay-pairing-parse')
        path = f'/relay/nodes/{member}/switch'
        assert self.post(path, lambda: self.click('relay-pairing-confirm')).ok
        self.click('relay-direction-close')

    def finish(self, a, b):
        self.cards_online(a, b)
        for member in (a, b):
            expect(self.page.locator(f'article[data-relay-member="{member}"] .relay-node-meta')).to_contain_text('本机连云端')
        self.el('relay-node-list').evaluate('el => el.scrollTop = 0')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        for theme in ('dark', 'light'):
            self.page.evaluate('(theme) => applyTheme(theme)', theme)
            for element in ('relay-nodes-dialog', 'relay-direction', 'relay-listen-dialog', 'relay-join-dialog'):
                if element == 'relay-direction':
                    self.page.locator(f'article[data-relay-member="{a}"] button[data-action="direction"]').click()
                if element == 'relay-listen-dialog':
                    self.click('relay-open-listen')
                if element == 'relay-join-dialog':
                    self.click('relay-add')
                box = self.el(element).bounding_box()
                assert box and box['x'] >= 0 and box['x'] + box['width'] <= 390
                assert self.el(element).evaluate('el => el.scrollWidth <= el.clientWidth')
                self.shot(f'{element}-390-{theme}')
                if element == 'relay-direction':
                    self.click('relay-direction-close')
                if element == 'relay-listen-dialog':
                    self.click('relay-listen-close')
                if element == 'relay-join-dialog':
                    self.click('relay-join-cancel')
        assert not self.errors, self.errors
