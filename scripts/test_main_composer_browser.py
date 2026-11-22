"""Exercise native main-composer editing and layout with isolated browser routes."""
import argparse
import base64
import hashlib
import json
import math
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from test_message_visibility_browser import Fixture, FRONTEND, OLD


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l2cAAAAASUVORK5CYII=')
TIMELINE = [OLD] + [dict(OLD, id=20 + i, content=f'Short message {i}') for i in range(1, 25)]


class ComposerFixture(Fixture):
    def __init__(self):
        super().__init__()
        self.messages = []
        self.uploads = 0

    def route(self, route):
        path = urlsplit(route.request.url).path
        if path == '/messages':
            route.fulfill(json={'messages': TIMELINE})
        elif path == '/desktop/events':
            route.fulfill(body=': test\n\n', content_type='text/event-stream')
        elif path == '/attachments' and route.request.method == 'POST':
            self.uploads += 1
            route.fulfill(json={'id': 'fixture-image'})
        else:
            if path == '/msg':
                self.messages.append(route.request.post_data_json)
            super().route(route)


# These caret assertions use short hard lines (each fits on a single visual row). Their
# geometry is calculated independently of the application's measurement textarea.
GEOMETRY = '''ta => {
  const history = document.querySelector('#msg-list');
  const rect = el => el.getBoundingClientRect().toJSON();
  const view = {top: 0, bottom: innerHeight, left: 0, right: innerWidth};
  const contains = (a, b) => a.top >= b.top - .5 && a.bottom <= b.bottom + .5
    && a.left >= b.left - .5 && a.right <= b.right + .5;
  const field = rect(ta), list = rect(history), style = getComputedStyle(ta);
  const send = rect(document.querySelector('#btn-send'));
  const attach = rect(document.querySelector('#composer .image-add'));
  const lines = ta.value.split('\\n');
  const cssLineHeight = parseFloat(style.lineHeight), padTop = parseFloat(style.paddingTop);
  // Some engines round each native textarea line before laying out the next one.
  const nativeLineHeight = (ta.scrollHeight - padTop - parseFloat(style.paddingBottom)) / lines.length;
  const lineHeight = ta.scrollHeight > ta.clientHeight && Math.abs(nativeLineHeight - cssLineHeight) < 1
    ? nativeLineHeight : cssLineHeight;
  const lineIndex = ta.value.slice(0, ta.selectionStart).split('\\n').length - 1;
  const visibleTop = field.top + ta.clientTop, visibleBottom = visibleTop + ta.clientHeight;
  const lineTop = visibleTop + padTop + lineIndex * lineHeight - ta.scrollTop;
  const lineBottom = lineTop + lineHeight;
  const messages = [...history.querySelectorAll('[data-message-id] .msg-body')]
    .filter(el => contains(rect(el), list) && contains(rect(el), view)).length;
  return {viewport: [innerWidth, innerHeight], field, send, attach, list,
    sendVisible: contains(send, view), attachVisible: contains(attach, view), messages,
    historyTop: history.scrollTop, historyExtent: history.scrollHeight - history.clientHeight,
    selection: [ta.selectionStart, ta.selectionEnd], focused: ta === document.activeElement,
    textTop: ta.scrollTop, textExtent: ta.scrollHeight - ta.clientHeight, clientHeight: ta.clientHeight,
    clientWidth: ta.clientWidth, scrollHeight: ta.scrollHeight,
    textWidth: ta.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
    lineHeight, lineTop, lineBottom, visibleTop, visibleBottom,
    lineVisible: lineTop >= visibleTop - .5 && lineBottom <= visibleBottom + .5,
    altEvent: window.composerAltEvent, theme: document.documentElement.dataset.theme,
    noPageOverflow: document.documentElement.scrollWidth <= innerWidth + 1};
}'''


def check_alt(field, value, caret):
    field.press('Alt+Enter')
    state = field.evaluate(GEOMETRY)
    assert state['altEvent'] == {'trusted': True, 'prevented': True}, state
    assert field.input_value() == value and state['selection'] == [caret, caret], state
    assert state['focused'] and state['lineVisible'], state
    return state


def clear(field):
    field.press('ControlOrMeta+A')
    field.press('Backspace')
    assert not field.input_value()


def run_case(page, fixture, viewport, theme, evidence):
    page.evaluate('''theme => {
      document.documentElement.dataset.theme = theme;
      if (innerWidth === 390) {
        for (const panel of document.querySelectorAll('.col-threads, .col-tasks, #layout > .resizer')) {
          panel.style.display = 'none';  // Isolate the message column from the existing three-panel layout.
        }
      }
      document.querySelector('#composer-text').addEventListener('keydown', e => {
        if (e.key === 'Enter' && e.altKey) window.composerAltEvent = {trusted: e.isTrusted, prevented: e.defaultPrevented};
      });
    }''', theme)
    width, height = viewport['width'], viewport['height']
    scenario = f'{width}x{height}-{theme}'
    field = page.locator('#composer-text')
    field.focus()
    empty = field.evaluate(GEOMETRY)
    assert empty['viewport'] == [width, height] and empty['theme'] == theme and empty['noPageOverflow'], empty
    blank = [check_alt(field, '\n' * count, count) for count in range(1, 5)]
    assert blank[1]['field']['height'] > empty['field']['height']
    assert blank[3]['field']['height'] > blank[1]['field']['height']
    field.fill('one\ntwo\nthree')
    before_fourth = field.evaluate(GEOMETRY)
    fourth = check_alt(field, 'one\ntwo\nthree\n', 14)
    assert fourth['field']['height'] > before_fourth['field']['height']
    page.screenshot(path=str(evidence / f'fourth-{scenario}.png'), animations='disabled', caret='initial')

    field.fill('word ' * 34)
    soft = field.evaluate(GEOMETRY)
    assert soft['field']['height'] > empty['field']['height'] and '\n' not in field.input_value(), soft
    if (width, height) == (1280, 800):
        page.locator('.col-threads').evaluate("el => { el.style.width = '460px'; }")
        page.wait_for_function("([w, h]) => { const el = document.querySelector('#composer-text'); "
                               'return el.clientWidth < w && el.offsetHeight > h; }',
                               arg=[soft['clientWidth'], soft['field']['height']])
        page.locator('.col-threads').evaluate("el => el.style.removeProperty('width')")
        page.wait_for_function("h => document.querySelector('#composer-text').offsetHeight === h",
                               arg=soft['field']['height'])
    clear(field)
    assert field.evaluate(GEOMETRY)['field']['height'] == empty['field']['height']

    lines = [f'Line {n:02d}' for n in range(1, 33)]
    draft = '\n'.join(lines)
    field.fill(draft)
    capped = field.evaluate(GEOMETRY)
    assert capped['textWidth'] > 90 and capped['textExtent'] > 0 and capped['field']['height'] >= soft['field']['height'], capped
    if (width, height) == (1280, 800):
        page.set_viewport_size({'width': width, 'height': 480})
        page.wait_for_function("h => document.querySelector('#composer-text').offsetHeight < h",
                               arg=capped['field']['height'])
        page.set_viewport_size(viewport)
        page.wait_for_function("h => document.querySelector('#composer-text').offsetHeight === h",
                               arg=capped['field']['height'])

    middle = sum(len(line) + 1 for line in lines[:13])
    field.evaluate('''(ta, mid) => {
      document.querySelector('#msg-list').scrollTop = 200;
      ta.setSelectionRange(mid, mid + 1);
      const s = getComputedStyle(ta);
      ta.scrollTop = Math.ceil(parseFloat(s.paddingTop) + 14 * parseFloat(s.lineHeight) - ta.clientHeight + 1);
    }''', middle)
    before = field.evaluate(GEOMETRY)
    assert before['lineVisible'] and before['selection'] == [middle, middle + 1], before
    after = check_alt(field, draft[:middle] + '\n' + draft[middle + 1:], middle + 1)
    assert before['field'] == after['field'] and before['historyTop'] == after['historyTop'], (before, after)
    assert 0 < after['textTop'] - before['textTop'] <= math.ceil(after['lineHeight']) + 3, (before, after)
    assert after['textTop'] < after['textExtent'], after
    assert after['sendVisible'] and after['attachVisible'] and after['historyExtent'] > 0 and after['messages'] >= 1, after
    assert fixture.posts == 0 and len(fixture.messages) == 0
    page.screenshot(path=str(evidence / f'capped-{scenario}.png'), animations='disabled', caret='initial')
    field.hover()
    page.mouse.wheel(0, 64)
    page.wait_for_function("top => document.querySelector('#composer-text').scrollTop > top", arg=after['textTop'])
    wheel = field.evaluate(GEOMETRY)
    assert all(wheel[k] == after[k] for k in ('send', 'attach', 'historyTop', 'list')), (after, wheel)

    # Existing point-name confirmation does not send; adding a real file still uses the same composer.
    field.fill('@bo')
    page.locator('#mention-pop .mp-item').first.wait_for(state='visible')
    field.press('Enter')
    assert field.input_value() == '@Bot（bot） ' and fixture.posts == 0, field.input_value()
    field.press('End')
    for _ in range(4):
        field.press('Alt+Enter')
    page.keyboard.insert_text('send probe')
    assert field.evaluate(GEOMETRY)['field']['height'] > empty['field']['height'] and fixture.posts == 0
    with page.expect_file_chooser() as chooser:
        page.locator('#composer .image-add').click()
    chooser.value.set_files({'name': 'fixture.png', 'mimeType': 'image/png', 'buffer': PNG})
    page.locator('#composer .image-drafts img').wait_for(state='visible')
    with page.expect_request(lambda r: r.method == 'POST' and urlsplit(r.url).path == '/msg'):
        field.press('Enter')
    page.wait_for_function("!document.querySelector('#composer-text').value && "
                           "!document.querySelector('#btn-send').disabled && "
                           "document.querySelector('#composer .image-toolbar').hidden")
    assert fixture.uploads == fixture.posts == 1
    assert fixture.messages[0]['text'] == '@bot \n\n\n\nsend probe' and fixture.messages[0]['attachment_ids'] == ['fixture-image']
    sent = field.evaluate(GEOMETRY)
    assert sent['field']['height'] == empty['field']['height'] and sent['textTop'] == 0, sent

    # Check the text-only clear path independently of attachment-toolbar and button resizing.
    field.fill('one\ntwo\nthree\nfour')
    before_clear = field.evaluate(GEOMETRY)
    assert before_clear['field']['height'] > empty['field']['height']
    field.press('Enter')
    page.wait_for_function("document.querySelector('#composer-text').value === ''")
    cleared = field.evaluate(GEOMETRY)
    assert fixture.posts == 2 and fixture.messages[1]['attachment_ids'] == []
    assert cleared['field']['height'] == empty['field']['height'] and cleared['textTop'] == 0, cleared
    return {'scenario': scenario, 'empty': empty, 'blank': blank, 'fourth': fourth, 'soft': soft,
            'middle_before': before, 'middle_after': after, 'wheel': wheel, 'sent': sent,
            'before_clear': before_clear, 'cleared': cleared}


def run(evidence, engine):
    if not evidence.is_absolute():
        raise ValueError('--evidence must be absolute')
    evidence.mkdir(parents=True, exist_ok=False)
    root = FRONTEND.parent
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    index_hash = hashlib.sha256((FRONTEND / 'index.html').read_bytes()).hexdigest()
    results, errors = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome') if engine == 'chrome' else getattr(pw, engine).launch()
        try:
            for theme in ('dark', 'light'):
                for width, height in ((1280, 800), (390, 800), (1280, 480)):
                    viewport = {'width': width, 'height': height}
                    with browser.new_context(viewport=viewport, color_scheme=theme, service_workers='block') as context:
                        context.on('page', lambda page: page.on('pageerror', lambda error: errors.append(str(error))))
                        fixture = ComposerFixture()
                        page = fixture.start(context)
                        results.append(run_case(page, fixture, viewport, theme, evidence))
                        print(f'PASS {engine} {width}x{height} {theme}', flush=True)
            assert not errors, errors
            report = {'commit': commit, 'index_sha256': index_hash, 'browser': engine,
                      'version': browser.version, 'cases': results, 'errors': errors}
            (evidence / 'results.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        finally:
            browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--browser', default='chrome', choices=('chrome', 'firefox', 'webkit'))
    args = parser.parse_args()
    run(args.evidence, args.browser)
