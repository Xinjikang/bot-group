"""Real Chromium quota and reload checks using production cache functions."""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'frontend/index.html').read_text(encoding='utf-8')
START = HTML.index('const THINKING_STORAGE_KEY')
CODE = HTML[START:HTML.index('window.addEventListener("pagehide"', START)]
FILL = '''() => {
    let n=0;
    while (localStorage.getItem('quota-' + n) !== null) n++;
    for (let size=512*1024; size>=1; size=Math.floor(size/2)) {
        while (true) {
            try { localStorage.setItem('quota-' + n, 'x'.repeat(size)); n++; }
            catch(e) { if(e.name !== 'QuotaExceededError') throw e; break; }
        }
    }
}'''


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page()
        page.route('**/*', lambda route: route.fulfill(body='<html><body>Isolated cache test</body></html>', content_type='text/html'))
        page.add_init_script('window.S = {thinkRuns:new Map(), thinkBubbles:new Map()};\n' + CODE + '''
            window.saveThinkingState = saveThinkingState;
            window.restoreThinkingState = restoreThinkingState;
            window.THINKING_STORAGE_KEY = THINKING_STORAGE_KEY;
        ''')
        warnings = []
        page.on('console', lambda msg: warnings.append(msg.text) if msg.type == 'warning' else None)
        page.goto('http://thinking-cache.test/')
        page.evaluate('''() => {
            const now = Date.now();
            S.thinkRuns.set('recent', {key:'recent', wake_started_at:now/1000,
                cacheUpdatedAt:now, finished:true, after_id:42, currentKey:'b', lastReplyId:9});
            S.thinkBubbles.set('b', {key:'b', sourceKey:'recent', entries:[{id:42, reasoning:'x'.repeat(1800000)}]});
            S.thinkRuns.set('expired', {key:'expired', wake_started_at:1,
                cacheUpdatedAt:now-8*86400000, finished:true});
            localStorage.setItem('group-engine.thinking.v2', JSON.stringify({runs:[...S.thinkRuns.values()], bubbles:[...S.thinkBubbles.values()]}));
            localStorage.setItem('user-preference','keep');
        }''')
        page.evaluate(FILL)
        assert page.evaluate('''() => {
            try {localStorage.setItem('probe','x'.repeat(4096));return false;}
            catch(e){return e.name==='QuotaExceededError';}
        }''')
        page.evaluate('saveThinkingState()')
        result = page.evaluate('''() => {
            const value=localStorage.getItem(THINKING_STORAGE_KEY), saved=JSON.parse(value);
            return {size:value.length, runs:saved.runs.map(r=>r.key), truncated:saved.bubbles[0].cacheTruncated,
                live:S.thinkBubbles.get('b').entries[0].reasoning.length, preference:localStorage.getItem('user-preference')};
        }''')
        assert result['size'] <= 512*1024 and result['runs'] == ['recent'], result
        assert result['truncated'] and result['live'] == 1800000 and result['preference'] == 'keep', result
        page.reload()
        page.evaluate('restoreThinkingState()')
        assert page.evaluate("S.thinkBubbles.get('b').entries[0].reasoning.length") > 0
        assert page.evaluate("S.thinkRuns.has('recent') && !S.thinkRuns.has('expired')")
        assert not warnings, warnings
        page.evaluate("localStorage.removeItem(THINKING_STORAGE_KEY)")
        page.evaluate(FILL)
        before = page.evaluate("JSON.stringify([...S.thinkBubbles.values()])")
        page.evaluate('saveThinkingState(); saveThinkingState()')
        assert page.evaluate('localStorage.getItem(THINKING_STORAGE_KEY)') is None
        assert page.evaluate('JSON.stringify([...S.thinkBubbles.values()])') == before
        assert len(warnings) == 1, warnings
        assert page.evaluate("localStorage.getItem('user-preference')") == 'keep'
        page.evaluate("Object.keys(localStorage).filter(k=>k.startsWith('quota-')).forEach(k=>localStorage.removeItem(k)); saveThinkingState()")
        assert page.evaluate('JSON.parse(localStorage.getItem(THINKING_STORAGE_KEY)).runs.length') == 1
        browser.close()
    print('PASS: full quota compaction/reload; zero space preserves live content, warns once, recovers after space freed')


if __name__ == '__main__':
    main()
