// jsdom tests of the shipped viewer.html: Chinese tool summaries, edit statistics, file viewer, Git review dialog.
// The page talks to a scripted in-memory server (no sockets, no model calls).
const fs = require('fs'), path = require('path'), assert = require('assert/strict');
// jsdom is a devDependency (npm ci). A missing dependency is a failure, never a skipped pass.
const { JSDOM, VirtualConsole } = require('jsdom');
const assets = path.join(__dirname, '..', 'assets');
const html = fs.readFileSync(path.join(assets, 'viewer.html'), 'utf8');
const scripts = [...html.matchAll(/<script src="([^"]+)"><\/script>/g)].map(m => fs.readFileSync(path.join(assets, m[1].replace(/^\//, '')), 'utf8'));
const pageHtml = html.replace(/<script src="[^"]+"><\/script>\n?/g, '');
const tick = (ms = 0) => new Promise(r => setTimeout(r, ms));
const settle = async (n = 10) => { for (let i = 0; i < n; i++) await tick(5); };
const respond = (body, status = 200) => ({ ok: status < 400, status, json: async () => body });

const upd = u => JSON.stringify({ type: 'sessionUpdate', data: { update: u } });
const call = (id, title, kind, input, extra = {}) => [upd({ sessionUpdate: 'tool_call', toolCallId: id, title, kind, rawInput: input, ...(extra.call || {}) }),
  ...(extra.done ? [upd({ sessionUpdate: 'tool_call_update', toolCallId: id, status: extra.status || 'completed', title, rawInput: input, ...extra.done })] : [])];
const cmd = (id, command, output = 'ok', status = 'completed') => call(id, 'Run Command', 'execute', { command, run_in_background: false },
  { done: { status, content: [{ type: 'content', content: { type: 'text', text: output } }], rawOutput: { output } } });

function conv(id, events, status = 'completed') {
  return { title: id, status, runs: [{ state: { id: 'r-' + id, agent_id: id, status, started_at: '2030-05-06T07:00:00+00:00', finished_at: '2030-05-06T07:00:05+00:00', exit_code: 0, prompt_forwarded: true },
    task: 'task ' + id, events: events.join('\n'), diagnostics: '', summary: null, temp: null }] };
}
function view(w, params) {
  const groups = [{ id: 'g', kind: 'codex', name: 'Proj', path: '/p', roots: ['/p'], exists: true, can_create: true, note: '',
    conversations: Object.entries(w.convs).map(([id, c]) => ({ key: 's1:' + id, source: 's1', id, title: c.title, status: c.status, role_id:c.role_id,role_name:c.role_name, created_at: '2030-05-06T07:00:00Z', activity: 1, cwd: '/p', turn_count: 1, deletable: true })) }];
  groups.push({ id: 'other', kind: 'other', name: '其他对话', path: '/daily', can_create: true, conversations: [] });
  const agent = params.get('agent'), c = w.convs[agent];
  const selected = c ? { id: agent, title: c.title, status: c.status, role_id:c.role_id,role_name:c.role_name,role_error:c.role_error, created_at: '2030-05-06T07:00:00Z', cwd: '/p', source: 's1', group: 'g', key: 's1:' + agent, config: c.config||null, context: null, provider:c.provider||'kiro' } : null;
  return { groups, selected, turns: c ? c.runs : [], root: '/logs', source_groups: { s1: 'g' }, default: { group: 'g', source: 's1' }, catalog: { status: 'ok', message: '' },
    viewer: { installation_root:w.installationRoot, active_tasks: 0, cleanup: {}, memory_mb: 1, account: null, providers:w.connections?.providers||w.providers,default_provider:w.connections?.default_provider||w.default_provider,connections_required:!!w.connections,
      composer_roles:w.roles?.roles.filter(role=>role.enabled) } };
}
function boot(w, options = {}) {
  const calls = [], errors = [], vc = new VirtualConsole();
  vc.on('jsdomError', e => errors.push(e));
  const server = (url, init) => {
    const u = new URL(url, 'http://127.0.0.1:47831'), q = u.searchParams;
    calls.push({ path: u.pathname, query: Object.fromEntries(q), post: !!(init && init.method === 'POST'), body:init?.body?JSON.parse(init.body):null });
    if (u.pathname === '/api') return respond(view(w, q));
    if (u.pathname === '/api/delete' && w.deleteConversation) return w.deleteConversation(JSON.parse(init.body));
    if (u.pathname === '/api/send' && w.send) return w.send(JSON.parse(init.body));
    if (u.pathname === '/api/tasks/stop') return w.taskStop?w.taskStop(JSON.parse(init.body)):respond({stopped:true});
    if (u.pathname === '/api/temp/overview' && w.cleanupOverview) return respond(w.cleanupOverview);
    if (u.pathname === '/api/temp/bulk' && w.cleanupAction) return w.cleanupAction(JSON.parse(init.body));
    if (u.pathname === '/api/roles') {
      if(init?.method==='POST')return w.roleAction?w.roleAction(JSON.parse(init.body)):respond({error:'save failed'},400);
      return respond(w.roles||{version:1,revision:'missing',roles:[]});
    }
    if (u.pathname === '/api/connections') {
      if(init?.method==='POST')return w.connectionAction? w.connectionAction(JSON.parse(init.body)):respond({ok:false,message:'not connected'},400);
      return respond(w.connections||{providers:[],default_provider:null});
    }
    if (u.pathname === '/api/models') return respond(w.models?w.models(q):{ status: 'unavailable', models: [], default: null });
    if (u.pathname === '/api/github/auth' && w.githubAuth) return w.githubAuth(init?.body ? JSON.parse(init.body).action : null);
    if (u.pathname.startsWith('/api/workspace') && w.workspace) return w.workspace(u, init);
    if (u.pathname.startsWith('/api/review')) {
      const project = q.get('scope') === 'project';
      const echo = { group: q.get('group'), source: project ? '' : q.get('source'), agent: project ? '' : q.get('agent') };
      const handler = w.review[u.pathname];
      const finish = r => respond({ ...echo, ...r.body }, r.status || 200);
      const result = handler ? handler(Object.fromEntries(q), echo) : { status: 404, body: { error: 'x' } };
      return result && typeof result.then === 'function' ? result.then(finish) : finish(result);
    }
    return respond({ error: 'unexpected ' + url }, 404);
  };
  const dom = new JSDOM(pageHtml, { url: 'http://127.0.0.1:47831/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(window) {
      if (options.selection) window.sessionStorage.setItem('kiro.selection.v1', JSON.stringify(options.selection));
      // jsdom exposes dialog.open but does not implement the native modal methods.
      window.HTMLDialogElement.prototype.showModal ??= function () { this.open = true; };
      window.HTMLDialogElement.prototype.close ??= function () { this.open = false; this.dispatchEvent(new window.Event('close')); };
      window.HTMLElement.prototype.scrollIntoView ??= function () {};
      window.matchMedia = query => ({matches:(options.width||1024)<=640,media:query,addEventListener(){},removeEventListener(){}});
      window.fetch = (url, init) => {
        const signal = init && init.signal;
        const result = Promise.resolve(server(String(url), init));
        if (!signal) return result;
        return new Promise((resolve, reject) => {
          const abort = () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
          if (signal.aborted) return abort();
          signal.addEventListener('abort', abort);
          result.then(resolve, reject);
        });
      };
      window.confirm = () => true;
      window.navigator.clipboard = { writeText: async text => { ctx.copied.push(text); } };
    } });
  const { window } = dom, { document } = window;
  const ctx = { dom, window, document, calls, errors, w, copied: [], $: id => document.getElementById(id), ev: code => window.eval(code),
    click: el => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })),
    key: (key, target) => (target || document).dispatchEvent(new window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })),
    poll: async () => { window.eval('clearTimeout(timer);refresh()'); await settle(); },
    openConv: async id => { ctx.click(document.querySelector('.run[data-id="' + id + '"]')); await settle(); },
    cards: () => [...document.querySelectorAll('.tool-row')],
    card: i => document.querySelectorAll('.tool-row')[i],
    title: i => document.querySelectorAll('.tool-row')[i].querySelector('.tool-name').textContent,
    stat: i => document.querySelectorAll('.tool-row')[i].querySelector('.tool-stat').textContent,
    btn: (root, label) => [...root.querySelectorAll('button')].find(b => b.textContent === label),
    ready: async () => { await new Promise(r => window.addEventListener('load', r)); for (const code of scripts) window.eval(code); await settle(20); return ctx; } };
  return ctx;
}
const newWorld = (convs, review = {}) => ({ convs, review });

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// ---- Chinese summaries -------------------------------------------------------------------------------
test('command summaries are specific and conservative', async () => {
  const t = await boot(newWorld({ c1: conv('c1', []) })).ready();
  const S = c => JSON.parse(JSON.stringify(t.ev('summarizeShell(' + JSON.stringify(c) + ')')));
  const title = c => S(c).title;
  const table = [
    ['python3 -m pytest -q tests', '运行Python测试'],
    ['cd /x/y && python3 -m unittest discover -s tests', '运行Python测试'],
    ['pytest', '运行Python测试'],
    ['/opt/venv/bin/python3.12 tests/test_review.py', '运行Python测试'],
    ['python3 scripts/build.py --fast', '执行Python脚本'],
    ['python3 - <<\'EOF\'\nimport os\nprint("rm -rf /; git push")\nEOF', '执行Python脚本'],
    ['python3 -c "print(1)"', '执行Python脚本'],
    ['ruff check .', '检查代码规范'],
    ['python3 -m flake8 src', '检查代码规范'],
    ['black --check .', '检查代码规范'],
    ['black .', '格式化代码'],
    ['mypy src', '检查类型'],
    ['git status --short', '检查Git改动'],
    ['git -C /repo diff --stat', '检查Git改动'],
    ['git log --oneline -5', '查看Git历史'],
    ['git push origin main', '推送Git提交'],
    ['git commit -m "x"', '提交Git改动'],
    ['cat README.md', '读取文件'],
    ['tail -30 independent-tests.log', '读取文件'],
    ['sed -n "1,20p" watch.py', '读取文件'],
    ['sed -i "s/a/b/" f.py', '就地修改文件'],
    ['grep -rn "foo" src | head -20', '搜索文本'],
    ['ls -la work', '列出目录'],
    ['find . -name "*.py"', '查找文件'],
    ['rm -rf /tmp/kiro-xyz', '清理临时文件'],
    ['rm -f work/tool-review/tmp/x.pyc', '清理临时文件'],
    ['rm -rf src/important', '删除文件'],
    ['mkdir -p a/b', '创建目录'],
    ['node tests/dom.cjs', '运行JS测试'],
    ['node build.js', '执行Node脚本'],
    ['npm test', '运行JS测试'],
    ['npm run lint', '检查代码规范'],
    ['npm install', '管理依赖'],
    ['curl -s http://127.0.0.1:1/x', '发送网络请求'],
    ['bash -c "pytest -q"', '运行Python测试'],
    ['bash run.sh', '执行shell脚本'],
    ['cd /tmp', '切换目录'],
    ['echo done', '输出文本'],
    ['cat > notes.txt <<\'EOF\'\nhello\nEOF', '写入文件'],
    ['echo hi > out.txt', '写入文件'],
  ];
  for (const [command, expected] of table) assert.equal(title(command), expected, command);
  // multi-action
  assert.equal(title('cd /w && cat a.txt; python3 -m pytest -q; git status'), '读取文件、运行Python测试、检查Git改动');
  assert.equal(title('cat task.txt | head -30; tail -5 run.log; grep -n "^def " w.py | head -200'), '读取文件、搜索文本');
  // unknown stays honest
  for (const c of ['frobnicate --all', 'my-tool run', '', '   ', 'cd /x && frobnicate']) assert.equal(title(c), c.includes('cd') ? '执行命令' : '执行命令', c);
  assert.equal(S('cat a.txt && frobnicate').unknown, true);
  assert.match(title('cat a.txt && frobnicate'), /^读取文件 等$/);
  assert.equal(S('echo hi > out.txt').mutates, true);
  assert.equal(S('cat README.md').mutates, false);
  assert.equal(S('python3 x.py').mutates, true); // arbitrary scripts may modify files
});

test('heredoc bodies, quotes and comments never create actions', async () => {
  const t = await boot(newWorld({ c1: conv('c1', []) })).ready();
  const S = c => JSON.parse(JSON.stringify(t.ev('summarizeShell(' + JSON.stringify(c) + ')')));
  const body = 'cat > /tmp/x.py <<\'EOF\'\nrm -rf /\ngit push --force\npytest\nEOF\necho after';
  assert.deepEqual(S(body).actions, ['写入文件']);
  assert.deepEqual(S('python3 - <<EOF\ngit commit -am x\nrm -rf /\nEOF').actions, ['执行Python脚本']);
  assert.deepEqual(S('echo "git push; rm -rf /"').actions, ['输出文本']);
  assert.deepEqual(S("echo 'a && pytest'").actions, ['输出文本']);
  assert.deepEqual(S('# rm -rf /\nls').actions, ['列出目录']);
  assert.deepEqual(S('cat <<-EOF\n\tgit push\n\tEOF\nls').actions, ['列出目录']);
  assert.deepEqual(S('python3 - <<\'PY\' | tail -3\nrm -rf /\nPY').actions, ['执行Python脚本']);
  assert.deepEqual(S('x=$(git push) && ls').actions, ['列出目录']); // substitution text is not a top-level action
  assert.equal(S('cat <<EOF\nunterminated rm -rf /\n').title, '输出文本');
});

test('non-command tools get Chinese titles and unknown ones keep theirs', async () => {
  const t = await boot(newWorld({ c1: conv('c1', []) })).ready();
  const T = tool => t.ev('toolSummary(' + JSON.stringify(tool) + ')');
  assert.equal(T({ title: 'Read File', kind: 'read', rawInput: { path: '/p/a' } }).title, '读取文件');
  assert.equal(T({ title: 'Write File', kind: 'edit', rawInput: { path: '/p/a', text: 'x' } }).title, '写入文件');
  assert.equal(T({ title: 'Replace in File', kind: 'edit', rawInput: {} }).title, '修改文件');
  assert.equal(T({ title: 'Mystery Tool', kind: 'other', rawInput: {} }).title, 'Mystery Tool');
  assert.equal(T({ kind: 'search', rawInput: {} }).title, '搜索内容');
  assert.equal(T({ title: 'Run Command', kind: 'execute', rawInput: {} }).title, '执行命令');
});

// ---- Edit statistics -----------------------------------------------------------------------------------
const WRITE_NEW = (id, p, text) => call(id, 'Write File', 'edit', { path: p, text }, { call: { status: 'in_progress', locations: [{ path: p }] },
  done: { locations: [{ path: p }], content: [{ type: 'diff', path: 'file://' + p, oldText: '', newText: text }], rawOutput: { message: 'Created the ' + p + ' file.' } } });
const REPLACE = (id, p, oldFile, newFile, extra = {}) => call(id, 'Replace in File', 'edit', { path: p, oldStr: 'x', newStr: 'y', replace_all: false },
  { call: { status: 'in_progress', locations: [{ path: p }] },
    done: { locations: [{ path: p }], content: [{ type: 'diff', path: 'file://' + p, oldText: oldFile, newText: newFile }], rawOutput: { message: 'Replaced text in ' + p }, ...extra } });

test('edit cards show provable +/- and path; failed and unrecorded are not invented', async () => {
  const oldFile = 'a\nb\nc\nd\ne\n', newFile = 'a\nB\nB2\nc\nd\n';
  const events = [
    ...WRITE_NEW('w1', '/p/new.py', 'l1\nl2\nl3\n'),
    ...REPLACE('r1', '/p/f.py', oldFile, newFile),
    // explicit oldText '' is a trusted empty old file, so the count is computed from the pair
    ...call('w2', 'Write File', 'edit', { path: '/p/over.py', text: 'q\nq\n' }, { call: { locations: [{ path: '/p/over.py' }] },
      done: { locations: [{ path: '/p/over.py' }], content: [{ type: 'diff', path: 'file:///p/over.py', oldText: '', newText: 'q\nq\n' }], rawOutput: { message: 'Updated /p/over.py' } } }),
    // failed
    ...call('w3', 'Write File', 'edit', { path: '/p/bad.py', text: 'zzz' }, { status: 'failed', call: { locations: [{ path: '/p/bad.py' }] }, done: { status: 'failed', content: [{ type: 'content', content: { type: 'text', text: 'Permission denied' } }], rawOutput: { message: 'Permission denied' } } }),
    // in progress (empty diff placeholder as seen in real logs)
    ...call('w4', 'Replace in File', 'edit', { path: '/p/wip.py', oldStr: 'a', newStr: 'b' }, { call: { status: 'in_progress', locations: [{ path: '/p/wip.py' }] },
      done: { status: 'pending', locations: [{ path: '/p/wip.py' }], content: [{ type: 'diff', path: '', oldText: '', newText: '' }] } }),
    // shell modifying files
    ...cmd('s1', "sed -i 's/a/b/' /p/x.py"),
    ...cmd('s2', 'python3 fix.py'),
    ...cmd('s3', 'git status --short'),
    // patch style
    ...call('p1', 'Apply Patch', 'edit', { patch: '*** Begin Patch\n*** Update File: src/a.py\n@@\n-old1\n-old2\n+new1\n+new2\n+new3\n context\n*** Add File: src/b.py\n+x\n+y\n*** End Patch' }, { done: {} }),
    // identical text
    ...REPLACE('r2', '/p/same.py', 'a\n', 'a\n'),
  ];
  const t = await boot(newWorld({ c1: conv('c1', events) })).ready();
  const [w1, r1, w2, w3, w4, s1, s2, s3, p1, r2] = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9];
  assert.equal(t.cards().length, 10);
  assert.equal(t.title(w1), '写入文件');
  assert.equal(t.stat(w1), '+3−0');
  assert.match(t.card(w1).textContent, /新建/);
  assert.equal(t.title(r1), '修改文件');
  assert.equal(t.stat(r1), '+2−2'); // b->B,B2 ; e removed
  assert.equal(t.stat(w2), '+2−0');
  assert.equal(t.stat(w3), ''); assert.match(t.card(w3).textContent, /工具执行失败，不计入修改行数/);
  assert.doesNotMatch(t.card(w3).textContent, /已修改/);
  assert.equal(t.stat(w4), ''); assert.match(t.card(w4).textContent, /还没有执行结果/);
  assert.equal(t.title(s1), '就地修改文件');
  assert.equal(t.stat(s1), ''); assert.match(t.card(s1).textContent, /本次行数未记录/);
  assert.ok(t.btn(t.card(s1), '打开改动'));
  assert.match(t.card(s2).textContent, /本次行数未记录/);
  assert.doesNotMatch(t.card(s3).textContent, /本次行数未记录/);
  assert.equal(t.title(p1), '应用补丁');
  assert.equal(t.stat(p1), '+5−2');
  assert.equal(t.stat(r2), '+0−0');
  // the whole-repo Git diff is never consulted for a tool card
  assert.equal(t.calls.filter(c => c.path.startsWith('/api/review')).length, 0);
  // the original title and raw input/output stay available
  assert.match(t.card(w1).textContent, /工具原始标题：Write File/);
  assert.equal(t.card(w1).querySelector('.tool-name').title, '工具原始标题：Write File');
  const raw = [...t.card(r1).querySelectorAll('h3')].map(h => h.textContent);
  assert.ok(raw.includes('输入') && raw.includes('结果'));
  assert.deepEqual(t.errors, []);
});

test('line diff is exact and builds a readable unified diff', async () => {
  const t = await boot(newWorld({ c1: conv('c1', []) })).ready();
  const D = (a, b) => JSON.parse(t.ev('JSON.stringify(lineDiff(' + JSON.stringify(a) + ',' + JSON.stringify(b) + ',true))'));
  assert.deepEqual([D('a\nb\n', 'a\nb\n').added, D('a\nb\n', 'a\nb\n').deleted], [0, 0]);
  assert.deepEqual([D('', 'x\ny').added, D('', 'x\ny').deleted], [2, 0]);
  assert.deepEqual([D('x\ny\n', '').added, D('x\ny\n', '').deleted], [0, 2]);
  const d = D('1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n', '1\n2\nX\n4\n5\n6\n7\n8\n9\n10\nEND\n');
  assert.deepEqual([d.added, d.deleted], [2, 1]);
  const unified = t.ev('unifiedFromOps("f.py", lineDiff("1\\n2\\n3\\n4\\n5\\n6\\n7\\n8\\n9\\n10\\n","1\\n2\\nX\\n4\\n5\\n6\\n7\\n8\\n9\\n10\\nEND\\n",true).ops)');
  assert.match(unified, /^--- a\/f.py\n\+\+\+ b\/f.py\n@@ -1,6 \+1,6 @@\n 1\n 2\n-3\n\+X\n 4\n 5\n 6\n@@ -8,3 \+8,4 @@\n 8\n/);
  assert.match(unified, /\+END\n$/);
  // too large to diff exactly: no number is invented
  const big = (n, tag) => Array.from({ length: n }, (_, i) => tag + i).join('\n');
  assert.equal(t.ev('lineDiff(' + JSON.stringify(big(3500, 'a')) + ',' + JSON.stringify(big(3500, 'b')) + ',false)'), null);
});

// ---- Viewer -----------------------------------------------------------------------------------------------
const review = (extra = {}) => ({
  '/api/review': () => ({ body: { kind: 'git', message: '', cwd: '/p', repo: { root: '/p', scope: '', branch: 'main', detached: false, unborn: false, head: 'abc1234' },
    areas: {
      staged: { entries: [{ path: 'a.txt', old_path: null, code: 'M', added: 2, deleted: 1, binary: false }, { path: 'new_name.txt', old_path: 'old_name.txt', code: 'R', added: 0, deleted: 0, binary: false }], truncated: false, hidden: 0, error: null },
      unstaged: { entries: [{ path: 'a.txt', old_path: null, code: 'M', added: 1, deleted: 0, binary: false }, { path: 'bin.dat', old_path: null, code: 'M', added: null, deleted: null, binary: true }, { path: 'gone.txt', old_path: null, code: 'D', added: 0, deleted: 3, binary: false }], truncated: false, hidden: 1, error: null },
      untracked: { entries: [{ path: '<img src=x onerror=alert(1)>.txt', old_path: null, code: '?', added: 1, deleted: 0, binary: false }], truncated: false, hidden: 0, error: null } } } }),
  '/api/review/diff': q => ({ body: { area: q.area, path: q.path, state: 'ok', code: 'M', diff: q.area === 'staged' ? 'diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,3 @@\n one\n-two\n+TWO\n+four\n' : 'diff --git a/a.txt b/a.txt\n@@ -1 +1,2 @@\n x\n+<script>window.pwned=1</script>\n', added: 2, deleted: 1, binary: q.path === 'bin.dat', truncated: false } }),
  '/api/review/file': q => q.path === 'outside' ? { status: 403, body: { error: '这个文件不在对话的工作目录内，面板不读取' } } : ({ body: q.path === 'missing.txt' ? { path: q.path, name: 'missing.txt', state: 'missing', message: '文件不存在（可能已删除或尚未创建）' }
    : q.path === 'big.bin' ? { path: q.path, name: 'big.bin', state: 'too_large', message: '文件过大', size: 5000000 }
    : { path: q.path, name: q.path.split('/').pop(), state: 'ok', text: 'line1\n<b>line2</b>\n\nline4', lines: 4, size: 24 } }),
  ...extra });
const simple = () => conv('c1', [...WRITE_NEW('w1', '/p/new.py', 'l1\nl2\n'), ...REPLACE('r1', '/p/f.py', 'a\nb\n', 'a\nc\n'), ...cmd('s1', 'python3 - <<\'EOF\'\nprint("hi")\nEOF'),
  ...call('rd', 'Read File', 'read', { path: '/p/a.txt' }, { call: { locations: [{ path: '/p/a.txt' }] }, done: { locations: [{ path: '/p/a.txt' }], rawOutput: { message: 'x' } } })]);

test('view file from a card: lines, safe text, copy, missing/too-large/outside states', async () => {
  const t = await boot(newWorld({ c1: simple() }, review())).ready();
  t.card(0).querySelector('details').open = true;
  t.click(t.btn(t.card(0), '查看文件')); await settle();
  assert.equal(t.$('review').hidden, false);
  const q = t.calls.filter(c => c.path === '/api/review/file').pop().query;
  assert.deepEqual([q.group, q.source, q.agent, q.path], ['g', 's1', 'c1', '/p/new.py']);
  assert.equal(t.$('rv-name').textContent, 'new.py');
  const rows = [...t.document.querySelectorAll('#rv-code .ln')];
  assert.equal(rows.length, 4);
  assert.equal(rows[0].querySelector('.no').textContent, '1');
  assert.equal(rows[1].querySelector('.tx').textContent, '<b>line2</b>');
  assert.equal(t.document.querySelector('#rv-code b'), null); // text, never markup
  assert.equal(t.$('rv-meta').textContent, '4 行 · 24 B');
  t.click(t.$('rv-copy')); await settle(); t.click(t.$('rv-copy-path')); await settle();
  assert.deepEqual(t.copied, ['line1\n<b>line2</b>\n\nline4', '/p/new.py']);
  // read-only: no editing controls
  assert.equal([...t.$('review').querySelectorAll('textarea,input,[contenteditable]')].some(
    node => !node.closest('[hidden],.pf-only')), false);
  t.key('Escape'); assert.equal(t.$('review').hidden, true);
  t.ev("openFileView('missing.txt')"); await settle();
  assert.match(t.$('rv-status').textContent, /文件不存在/);
  assert.equal(t.document.querySelectorAll('#rv-code .ln').length, 0);
  t.ev("openFileView('big.bin')"); await settle();
  assert.match(t.$('rv-status').textContent, /文件过大/);
  t.ev("openFileView('outside')"); await settle();
  assert.match(t.$('rv-status').textContent, /不在对话的工作目录内/);
  t.key('Escape');
  assert.deepEqual(t.errors, []);
});

test('tool diff and command script open locally without any server read', async () => {
  const t = await boot(newWorld({ c1: simple() }, review())).ready();
  const before = t.calls.length;
  t.click(t.btn(t.card(1), '查看本次修改')); await settle();
  const rows = [...t.document.querySelectorAll('#rv-code .ln')].map(r => r.className.replace('ln ', '') + ':' + r.querySelector('.tx').textContent);
  assert.ok(rows.includes('del:-b') && rows.includes('add:+c'), rows.join('|'));
  assert.equal(t.$('rv-meta').textContent, '+1 −1');
  t.key('Escape');
  t.click(t.btn(t.card(2), '查看脚本')); await settle();
  assert.equal(t.$('review-title').textContent, '命令脚本');
  assert.deepEqual([...t.document.querySelectorAll('#rv-code .tx')].map(n => n.textContent), ["python3 - <<'EOF'", 'print("hi")', 'EOF']);
  t.click(t.$('rv-copy')); await settle();
  assert.equal(t.copied.pop(), "python3 - <<'EOF'\nprint(\"hi\")\nEOF");
  t.key('Escape');
  assert.equal(t.calls.length, before);
});

test('Git review: categories, stats, branch, disclaimer, per-category diff, current text, XSS', async () => {
  const t = await boot(newWorld({ c1: simple() }, review())).ready();
  assert.equal(t.$('review-open').disabled, false);
  t.$('prompt').value = 'half typed'; t.$('prompt').dispatchEvent(new t.window.Event('input', { bubbles: true }));
  const scrollBefore = t.$('scroll').scrollTop;
  t.$('review-open').focus();
  t.click(t.$('review-open')); await settle();
  assert.equal(t.$('review').hidden, false);
  assert.equal(t.$('review-open').getAttribute('aria-expanded'), 'true');
  assert.match(t.$('review-note').textContent, /可能包含其他任务或你自己已有的修改，不代表这条对话做过的修改/);
  assert.match(t.$('rv-list').textContent, /分支：main/);
  const groups = [...t.document.querySelectorAll('.cl-group')].map(g => g.querySelector('h3').textContent);
  assert.match(groups[0], /已暂存（index） · 2\+2−1/);
  assert.match(groups[1], /未暂存（工作区） · 3\+1−3/);
  assert.match(groups[2], /未跟踪 · 1/);
  assert.match(t.$('rv-list').textContent, /old_name\.txt → new_name\.txt/);
  assert.match(t.$('rv-list').textContent, /二进制/);
  assert.match(t.$('rv-list').textContent, /已隐藏 1 个凭据\/敏感路径/);
  assert.equal(t.document.querySelector('#rv-list img'), null); // hostile file name stays text
  assert.match(t.$('rv-list').textContent, /<img src=x onerror=alert\(1\)>\.txt/);
  // staged a.txt and unstaged a.txt are different requests
  const rows = [...t.document.querySelectorAll('.cl-row')];
  t.click(rows[0]); await settle();
  let q = t.calls.filter(c => c.path === '/api/review/diff').pop().query;
  assert.deepEqual([q.area, q.path], ['staged', 'a.txt']);
  assert.ok([...t.document.querySelectorAll('#rv-code .ln.add')].some(r => r.textContent.includes('+TWO')));
  t.click(t.document.querySelectorAll('.cl-row')[2]); await settle();
  q = t.calls.filter(c => c.path === '/api/review/diff').pop().query;
  assert.deepEqual([q.area, q.path], ['unstaged', 'a.txt']);
  assert.equal(t.document.querySelector('#rv-code script'), null);
  assert.ok([...t.document.querySelectorAll('#rv-code .tx')].some(n => n.textContent === '+<script>window.pwned=1</script>'));
  assert.equal(t.window.pwned, undefined);
  // binary: no diff, no counts
  t.click(t.document.querySelectorAll('.cl-row')[3]); await settle();
  assert.match(t.$('rv-status').textContent, /二进制文件，不显示 diff，也不统计行数/);
  // current text tab
  t.click(t.$('rv-tab-text')); await settle();
  q = t.calls.filter(c => c.path === '/api/review/file').pop().query;
  assert.equal(q.path, 'bin.dat');
  t.click(t.$('rv-tab-diff')); await settle();
  // composer draft, selection and scroll are untouched; focus returns to the opener
  assert.equal(t.$('prompt').value, 'half typed');
  t.key('Escape');
  assert.equal(t.$('review').hidden, true);
  assert.equal(t.document.activeElement, t.$('review-open'));
  assert.equal(t.$('prompt').value, 'half typed');
  assert.equal(t.$('scroll').scrollTop, scrollBefore);
  assert.equal(t.document.querySelector('main').hasAttribute('inert'), false);
  assert.equal(t.$('sidebar').hasAttribute('inert'), false);
  assert.deepEqual(t.errors, []);
});

test('project tabs preserve folder creation and switch Diff layout without a new request', async () => {
  const w = newWorld({}, review());
  const draft = '笔记';
  const before = { ok: true, group: 'g', cwd: '/p', root_revision: 'root' };
  const entries = { '': [{ kind: 'directory', name: 'docs' }, { kind: 'file', name: 'a.txt' }, { kind: 'file', name: 'index.D.TS' }], docs: [] };
  w.workspace = u => {
    if (u.pathname === '/api/workspace') {
      entries.docs.push({kind:'directory', name:draft}); entries['docs/'+draft] = [];
      return respond(before);
    }
    if (u.pathname === '/api/workspace/file') return respond({ ...before, state: 'binary' });
    return respond({ ...before, entries: entries[u.searchParams.get('path')] });
  };
  const t = await boot(w).ready();
  t.document.querySelector('[data-browse="g"]').click(); await settle();
  const row = name => t.document.querySelector('[data-path="'+name+'"]');
  assert.equal(row('index.D.TS').querySelector('.pf-file-icon').textContent, 'TS');
  row('index.D.TS').click(); await settle();
  assert.equal(t.$('pf-file-tabs').querySelector('.active .pf-file-icon').textContent, 'TS');
  const action = (file, name) => t.document.querySelector('[data-path="'+file+'"] [data-tree-action="'+name+'"]');
  action('a.txt', 'copy').click(); action('docs', 'copy').click(); await settle();
  assert.deepEqual(t.copied, ['/p/a.txt', '/p/docs']);
  action('docs', 'directory').click(); await settle();
  assert.ok(t.$('pf-new-name').closest('#pf-tree .pf-create-slot'));
  t.$('pf-new-name').value = draft;
  t.$('pf-tab-git').click(); await settle();
  assert.deepEqual([...t.$('rv-list').querySelectorAll('summary span:first-of-type')].map(n=>n.textContent), ['已暂存','未暂存','未跟踪']);
  t.$('rv-list').querySelector('.cl-row').click(); await settle();
  const iconShape = container => container.querySelector('.pf-file-icon path').getAttribute('d');
  assert.equal(iconShape(t.$('rv-name')), iconShape(row('a.txt')));
  assert.equal(t.$('rv-code').querySelector('.pf-diff-cell.add .tx').textContent, 'TWO');
  const rail = t.$('rv-diff-overview');
  assert.equal(rail.querySelectorAll('rect.add').length, 1); // TWO and four are one block.
  const diffReads = t.calls.filter(c=>c.path==='/api/review/diff').length;
  t.$('pf-split').click();
  assert.match(t.$('rv-code').querySelector('.ln.add').textContent, /\+TWO/);
  assert.equal(rail.querySelectorAll('rect.add').length, 1);
  assert.equal(t.calls.filter(c=>c.path==='/api/review/diff').length, diffReads);
  t.$('pf-tab-files').click();
  assert.equal(t.$('pf-new-name').value, draft);
  t.$('pf-new-ok').click(); await settle();
  assert.deepEqual(t.calls.find(c=>c.path==='/api/workspace' && c.post)?.body, {
    group:'g', root_revision:'root', action:'new_directory', path:'docs/'+draft
  });
  assert.equal(t.document.querySelector('[data-path="docs/笔记"]').getAttribute('aria-selected'), 'true');
  assert.deepEqual(t.errors, []);
});

test('project Diff can show the whole file past the old row limit in both layouts', async () => {
  const longDiff = ['@@ -1,6200 +1,6200 @@',
    ...Array.from({length:6200}, (_,i)=>i===1550?'-old line\n+changed line':i===5100?'-late old\n+late change':' line '+(i+1))].join('\n')+'\n';
  const w = newWorld({}, review({'/api/review/diff':()=>({body:{state:'ok',diff:longDiff,added:2,deleted:2}})}));
  const t = await boot(w).ready();
  t.document.querySelector('[data-browse="g"]').click(); await settle();
  t.$('pf-tab-git').click(); await settle();
  t.$('rv-list').querySelector('.cl-row').click(); await settle();
  const rail=t.$('rv-diff-overview'),viewer=t.$('rv-code');
  assert.equal(rail.hidden,false);
  assert.equal(rail.querySelectorAll('rect.del').length,2);
  assert.equal(rail.querySelectorAll('rect.add').length,2);
  assert.equal(viewer.querySelector('.pf-diff-cell.add .tx').textContent,'changed line');
  assert.equal(viewer.textContent.includes('late change'),false);
  rail.click();
  assert.equal(viewer.textContent.includes('late change'),true);
  const reveal = () => {
    const code = t.$('rv-code');
    for(let i=0;i<25;i++)code.querySelector('.rv-diff-more')?.click();
    assert.equal(code.querySelector('.rv-diff-more'),null);
    return code;
  };
  let code = reveal();
  assert.equal(code.querySelector('.pf-diff-cell.add .tx').textContent,'changed line');
  const last = code.querySelector('.pf-diff-split').lastElementChild.lastElementChild;
  assert.deepEqual([...last.querySelectorAll('.pf-diff-cell .tx')].map(n=>n.textContent),['line 6200','line 6200']);
  t.$('pf-split').click();
  code = reveal();
  assert.equal(code.querySelector('.ln.add .tx').textContent,'+changed line');
  assert.equal(code.querySelector('.rv-diff').lastElementChild.lastElementChild.querySelector('.tx').textContent,' line 6200');
  t.$('rv-tab-text').click();
  assert.equal(rail.hidden,true);
  await settle();
  assert.deepEqual(t.errors,[]);
  t.dom.window.close();
});

test('project Git actions move the selected diff, commit the index and update the branch label', async () => {
  const w = newWorld({}, review());
  const get = w.review['/api/review'];
  let staged = false, committed = false, branch = 'main';
  w.review['/api/review'] = () => {
    const state = get();
    const { body } = state;
    body.repo.branch = branch;
    body.controls = { root_revision: 'root', branches: ['feature','main'], index: staged ? 'index' : '' };
    const entry = body.areas.unstaged.entries[0];
    for (const [area, group] of Object.entries(body.areas)) {
      group.entries = !committed&&area===(staged?'staged':'unstaged')?[entry]:[];
      group.hidden = 0;
    }
    return state;
  };
  w.workspace = (url, init) => {
    if (url.pathname.endsWith('/git')) {
      const {action, name} = JSON.parse(init.body);
      if (action==='stage') staged = true;
      if (action==='commit') { committed = true; staged = false; }
      if (action==='switch') branch = name;
    }
    return respond({ok:true,group:'g',cwd:'/p',root_revision:'root',entries:[],head:'def1234'});
  };
  const t = await boot(w).ready();
  t.document.querySelector('[data-browse="g"]').click(); await settle();
  t.$('pf-tab-git').click(); await settle();
  t.$('rv-list').querySelector('.cl-row').click(); await settle();
  t.$('rv-list').querySelector('li [data-git-index="stage"]').click(); await settle();
  const posts = () => t.calls.filter(call=>call.path==='/api/workspace/git').map(call=>call.body);
  assert.deepEqual(posts()[0], {group:'g',root_revision:'root',action:'stage',head:'abc1234',branch:'main',area:'unstaged',paths:['a.txt'],all:false});
  assert.match(t.$('rv-meta').textContent,/已暂存/);
  assert.ok(t.$('rv-list').querySelector('li [data-git-index="unstage"]'));
  const message = t.$('pf-commit-message');
  assert.equal(t.$('pf-commit').disabled,true);
  message.value='Commit staged only';
  message.dispatchEvent(new t.window.Event('input',{bubbles:true}));
  t.$('pf-commit').click(); await settle();
  assert.equal(posts()[1].index,'index');
  assert.equal(posts()[1].message,'Commit staged only');
  assert.equal(message.value,'');
  t.$('pf-git-branches').click();
  const branchSelect=t.$('pf-branch-select');
  branchSelect.value='feature';
  branchSelect.dispatchEvent(new t.window.Event('change',{bubbles:true}));
  t.$('pf-branch-switch').click(); await settle();
  assert.equal(posts()[2].name,'feature');
  assert.equal(t.$('pf-branch').textContent,'feature');
  assert.deepEqual(t.errors,[]);
});

test('the Git toolbar confirms publication and updates the upstream counts', async () => {
  const w = newWorld({}, review());
  const get = w.review['/api/review'];
  let upstream = false;
  const url = 'https://github.com/demo/example';
  w.review['/api/review'] = () => {
    const state = get();
    state.body.controls = { root_revision:'root', branches:['main'], sync:{
      url, remote:'origin', branch:'main', upstream, ahead:upstream?0:null, behind:upstream?1:null, push_ok:true
    }};
    return state;
  };
  w.workspace = (request, init) => {
    if (request.pathname.endsWith('/git')) upstream = true;
    return respond({ok:true, group:'g', cwd:'/p', root_revision:'root', entries:[]});
  };
  const t = await boot(w).ready();
  t.document.querySelector('[data-browse="g"]').click(); await settle();
  t.$('pf-tab-git').click(); await settle();
  assert.equal(t.$('pf-git-web').href,url);
  assert.equal(t.$('pf-git-pull').disabled,true);
  assert.match(t.$('pf-sync-target').textContent,/未发布/);
  const posts = () => t.calls.filter(call=>call.path==='/api/workspace/git');
  t.window.confirm = question => { assert.match(question,/demo\/example.*main/); return false; };
  t.$('pf-git-push').click(); await settle();
  assert.equal(posts().length,0);
  t.window.confirm = () => true;
  t.$('pf-git-push').click(); await settle();
  assert.deepEqual(posts()[0].body.target,[url,'origin','main',false]);
  assert.equal(posts()[0].body.action,'push');
  assert.equal(t.$('pf-git-pull').disabled,false);
  assert.equal(t.$('pf-git-push').disabled,true);
  assert.match(t.$('pf-sync-target').textContent,/↓1 ↑0/);
  assert.deepEqual(t.errors,[]);
});

test('the file tree shows Git changes, marks parent folders, and refreshes ignored entries', async () => {
  const area = entries => ({entries, truncated:false, hidden:0});
  const tracked = path => ({path, code:'M'});
  let changed = true;
  const w = newWorld({}, {'/api/review':()=>({body:{
    kind:'git', repo:{branch:'main',root:'/p'}, areas:{
      staged:area(changed?[{path:'stage.txt',code:'A'}]:[]),
      unstaged:area(changed?[tracked('tracked.txt'),tracked('sub/nested.txt')]:[]),
      untracked:area(changed?[{path:'new.txt',code:'?'}]:[])
    }
  }})});
  w.workspace = u => respond({ok:true,group:'g',cwd:'/p',root_revision:'root',entries:
    u.searchParams.get('path')==='sub'?[{name:'nested.txt',kind:'file'}]:[
      {name:'sub',kind:'directory'}, {name:'cache',kind:'directory',ignored:changed},
      ...['tracked.txt','stage.txt','new.txt','tmp.log'].map(name=>({name,kind:'file',ignored:changed&&name==='tmp.log'}))
    ]});
  const t = await boot(w).ready();
  t.document.querySelector('[data-browse="g"]').click(); await settle();
  const row = path => t.document.querySelector('[data-path="'+path+'"]');
  const badge = path => row(path).querySelector('.pf-git-mark')?.textContent;
  assert.deepEqual(['tracked.txt','stage.txt','new.txt','sub'].map(badge),['M','A','U','●']);
  assert.equal(row('cache').dataset.git,'ignored');
  assert.equal(row('tmp.log').getAttribute('aria-label'),'tmp.log，Git 已忽略');
  row('sub').click(); await settle();
  assert.equal(badge('sub/nested.txt'),'M');
  changed=false;
  t.$('pf-refresh').click(); await settle();
  assert.equal(t.$('pf-tree').querySelector('[data-git]'),null);
  assert.deepEqual(t.errors,[]);
});

test('file previews are replaced, pinned tabs persist, and late reads cannot steal the editor', async () => {
  const w = newWorld({}, review()), base = { ok: true, group: 'g', cwd: '/p', root_revision: 'root' };
  let finishA;
  w.workspace = u => {
    if (u.pathname === '/api/workspace/tree') return respond({ ...base, entries:['a.txt','b.txt','c.txt'].map(name=>({kind:'file',name})) });
    const name = u.searchParams.get('path');
    if(name==='a.txt')return new Promise(resolve=>{finishA=()=>resolve(respond({...base,state:'binary',message:'A 已加载'}))});
    return respond({...base,state:'binary',message:name+' 已加载'});
  };
  const t = await boot(w).ready();
  t.document.querySelector('[data-browse="g"]').click(); await settle();
  const row = name => t.document.querySelector('[data-path="'+name+'"]');
  const tab = name => t.document.querySelector('[data-file-tab="'+name+'"]');
  row('a.txt').click();
  assert.ok(tab('a.txt').classList.contains('preview'));
  row('a.txt').dispatchEvent(new t.window.MouseEvent('dblclick',{bubbles:true}));
  assert.equal(tab('a.txt').classList.contains('preview'),false);
  row('b.txt').click(); await settle();
  row('c.txt').click(); await settle();
  assert.equal(tab('b.txt'),null);
  assert.ok(tab('a.txt'));
  assert.equal(tab('c.txt').querySelector('[role=tab]').getAttribute('aria-selected'),'true');
  finishA(); await settle();
  assert.equal(t.$('pf-placeholder').textContent,'c.txt 已加载');
  tab('a.txt').querySelector('[role=tab]').click();
  assert.equal(t.$('pf-placeholder').textContent,'A 已加载');
  tab('a.txt').querySelector('.pf-tab-close').click();
  assert.equal(tab('a.txt'),null);
  assert.equal(tab('c.txt').querySelector('[role=tab]').getAttribute('aria-selected'),'true');
  assert.deepEqual(t.errors,[]);
});

test('non-Git directory shows an empty state and file viewing still works', async () => {
  const w = newWorld({ c1: simple() }, review({ '/api/review': () => ({ body: { kind: 'nogit', message: '这个目录不在 Git 仓库中', cwd: '/p', repo: null, areas: {} } }) }));
  const t = await boot(w).ready();
  t.click(t.$('review-open')); await settle();
  assert.match(t.$('rv-list').textContent, /这个目录不是 Git 仓库/);
  assert.match(t.$('rv-list').textContent, /仍可在工具卡片里点“查看文件”/);
  t.key('Escape');
  t.click(t.btn(t.card(0), '查看文件')); await settle();
  assert.equal(t.document.querySelectorAll('#rv-code .ln').length, 4);
  t.key('Escape');
});

test('detached HEAD, unborn branch, subdirectory scope and area errors are explained', async () => {
  const base = { kind: 'git', message: '', cwd: '/p/sub', areas: { staged: { entries: [], truncated: false, hidden: 0, error: null }, unstaged: { entries: [], truncated: false, hidden: 0, error: 'Git 读取超时，已停止' }, untracked: { entries: [], truncated: false, hidden: 0, error: null } } };
  const w = newWorld({ c1: simple() }, review({ '/api/review': () => ({ body: { ...base, repo: { root: '/p', scope: 'sub', branch: '', detached: true, unborn: false, head: 'deadbeef1234' } } }) }));
  const t = await boot(w).ready();
  t.click(t.$('review-open')); await settle();
  assert.match(t.$('rv-list').textContent, /游离 HEAD（deadbeef1234）/);
  assert.match(t.$('rv-list').textContent, /只显示子目录 sub 内的改动/);
  assert.match(t.$('rv-list').textContent, /Git 读取超时，已停止/);
  t.key('Escape');
  w.review['/api/review'] = () => ({ body: { ...base, areas: { ...base.areas, unstaged: base.areas.staged }, repo: { root: '/p', scope: '', branch: 'main', detached: false, unborn: true, head: '' } } });
  t.click(t.$('review-open')); await settle();
  assert.match(t.$('rv-list').textContent, /main（尚无提交）/);
  assert.match(t.$('rv-list').textContent, /当前工作区没有未提交的改动/);
  t.key('Escape');
});

test('server refusals and failures are shown, never swallowed', async () => {
  const w = newWorld({ c1: simple() }, review({ '/api/review': () => ({ status: 409, body: { error: '对话所属项目已变化，请刷新页面' } }) }));
  const t = await boot(w).ready();
  t.click(t.$('review-open')); await settle();
  assert.match(t.$('rv-list').textContent, /对话所属项目已变化/);
  t.key('Escape');
});

test('polling neither rebuilds an open diff nor the cards; buttons keep working', async () => {
  const t = await boot(newWorld({ c1: simple() }, review())).ready();
  t.card(0).querySelector('details').open = true;
  const cardNode = t.card(0), button = t.btn(cardNode, '查看文件');
  t.click(t.$('review-open')); await settle();
  t.click(t.document.querySelectorAll('.cl-row')[0]); await settle();
  const code = t.$('rv-code'), firstRow = code.querySelector('.ln'), listRow = t.document.querySelector('.cl-row');
  for (let i = 0; i < 3; i++) await t.poll();
  assert.equal(t.$('rv-code'), code);
  assert.equal(code.querySelector('.ln'), firstRow);
  assert.equal(t.document.querySelector('.cl-row'), listRow);
  t.key('Escape');
  assert.equal(t.card(0), cardNode);
  assert.equal(t.card(0).querySelector('details').open, true);
  assert.equal(t.btn(t.card(0), '查看文件'), button);
  t.click(button); await settle();
  assert.equal(t.$('review').hidden, false);
  t.key('Escape');
  // a changed tool record rebuilds its body, and the fresh buttons still work
  t.w.convs.c1 = conv('c1', [...WRITE_NEW('w1', '/p/new.py', 'l1\nl2\nl3\n'), ...REPLACE('r1', '/p/f.py', 'a\nb\n', 'a\nc\n')]);
  await t.poll();
  assert.equal(t.stat(0), '+3−0');
  t.click(t.btn(t.card(0), '查看文件')); await settle();
  assert.equal(t.$('review').hidden, false);
  t.key('Escape');
  assert.deepEqual(t.errors, []);
});

test('Escape closes only the review, Tab stays inside it, background is inert', async () => {
  const t = await boot(newWorld({ c1: simple() }, review())).ready();
  t.click(t.$('review-open')); await settle();
  assert.equal(t.document.activeElement, t.$('review-close'));
  assert.ok(t.document.querySelector('main').hasAttribute('inert') && t.$('sidebar').hasAttribute('inert'));
  const focusables = [...t.$('review').querySelectorAll('button,[tabindex="0"]')].filter(n => !n.disabled && !n.hidden && !n.closest('[hidden]'));
  focusables[focusables.length - 1].focus();
  t.key('Tab', focusables[focusables.length - 1]);
  assert.equal(t.document.activeElement, focusables[0]);
  assert.equal(t.$('settings').hidden, true);
  t.key('Escape');
  assert.equal(t.$('review').hidden, true);
  assert.equal(t.$('settings').hidden, true);
  // settings still open and close normally afterwards, and cannot be stacked on the review
  t.click(t.$('settings-open')); assert.equal(t.$('settings').hidden, false);
  t.ev("openReview('changes')"); assert.equal(t.$('review').hidden, true);
  t.key('Escape'); assert.equal(t.$('settings').hidden, true);
  // the button is disabled without a conversation
  t.click(t.document.querySelector('.group-new')); await settle();
  assert.equal(t.$('review-open').disabled, true);
});

test('switching conversations while a request is in flight never shows the old answer', async () => {
  const slow = {};
  const w = newWorld({ c1: simple(), c2: conv('c2', []) }, review({
    '/api/review': (q, echo) => echo.agent === 'c1' ? new Promise(resolve => { slow.resolve = () => resolve({ body: { kind: 'git', message: '', cwd: '/p', repo: { root: '/c1-secret', scope: '', branch: 'c1-branch', detached: false, unborn: false, head: '' }, areas: { staged: { entries: [], truncated: false, hidden: 0, error: null }, unstaged: { entries: [], truncated: false, hidden: 0, error: null }, untracked: { entries: [], truncated: false, hidden: 0, error: null } } } }); })
      : { body: { kind: 'git', message: '', cwd: '/p2', repo: { root: '/c2', scope: '', branch: 'c2-branch', detached: false, unborn: false, head: '' }, areas: { staged: { entries: [], truncated: false, hidden: 0, error: null }, unstaged: { entries: [], truncated: false, hidden: 0, error: null }, untracked: { entries: [], truncated: false, hidden: 0, error: null } } } } }));
  const t = await boot(w).ready();
  t.click(t.$('review-open')); await settle();
  assert.match(t.$('rv-list').textContent, /正在读取/);
  // the conversation changes underneath (as when it is deleted or auto-cleaned): the dialog closes
  t.ev("select({group:'g',source:'s1',agent:'c2'})"); await settle();
  assert.equal(t.$('review').hidden, true);
  slow.resolve(); await settle();
  assert.equal(t.$('rv-list').textContent, '');
  assert.equal(t.$('review').hidden, true);
  // re-open on c2: only c2 data
  await settle(); t.click(t.$('review-open')); await settle();
  assert.match(t.$('rv-list').textContent, /c2-branch/);
  assert.doesNotMatch(t.$('rv-list').textContent, /c1/);
  t.key('Escape');
  assert.deepEqual(t.errors, []);
});

test('a slower earlier request cannot overwrite a newer one (refresh while loading)', async () => {
  const pending = [];
  const entry = name => ({ path: name, old_path: null, code: 'M', added: 1, deleted: 0, binary: false });
  const area = names => ({ entries: names.map(entry), truncated: false, hidden: 0, error: null });
  const w = newWorld({ c1: simple() }, review({ '/api/review': () => new Promise(resolve => pending.push(names => resolve({ body: { kind: 'git', message: '', cwd: '/p', repo: { root: '/p', scope: '', branch: 'main', detached: false, unborn: false, head: '' }, areas: { staged: area(names), unstaged: area([]), untracked: area([]) } } }))) }));
  const t = await boot(w).ready();
  t.click(t.$('review-open')); await settle();
  t.click(t.$('review-refresh')); await settle();
  assert.equal(pending.length, 2);
  pending[1](['new.txt']); await settle();
  pending[0](['old.txt']); await settle();
  assert.match(t.$('rv-list').textContent, /new\.txt/);
  assert.doesNotMatch(t.$('rv-list').textContent, /old\.txt/);
  t.key('Escape');
});


// ---- review round 2 ----------------------------------------------------------------------------------------
const E = (t, tool) => JSON.parse(t.ev('JSON.stringify(editInfo(' + JSON.stringify({ status: 'completed', ...tool }) + ',true))'));
const P = (t, text) => JSON.parse(t.ev('JSON.stringify(parsePatchText(' + JSON.stringify(text) + '))'));
const diffItem = (extra, msg) => ({ title: 'Write File', kind: 'edit', locations: [{ path: '/p/a' }], content: [{ type: 'diff', path: 'file:///p/a', ...extra }], rawOutput: { message: msg || 'Updated /p/a' } });

test('ACP diff items: missing fields are not empty text; only explicit pairs (or a reliable create) are counted', async () => {
  const t = await boot(newWorld({ c1: conv('c1', []) })).ready();
  const f = tool => E(t, tool).files[0];
  assert.deepEqual([f(diffItem({ oldText: 'a\nb\n', newText: 'a\nc\n' })).exact, E(t, diffItem({ oldText: 'a\nb\n', newText: 'a\nc\n' })).added], [true, 1]);
  // explicit empty old text is a trusted empty file
  let r = E(t, diffItem({ oldText: '', newText: 'x\ny\n' })); assert.deepEqual([r.exact, r.added, r.deleted], [true, 2, 0]);
  // explicit empty new text really deletes the content
  r = E(t, diffItem({ oldText: 'x\ny\n', newText: '' })); assert.deepEqual([r.exact, r.added, r.deleted], [true, 0, 2]);
  // missing newText is NOT a deletion
  r = E(t, diffItem({ oldText: 'x\ny\n' })); assert.equal(r.exact, false); assert.equal(r.some, false); assert.equal(r.files[0].added, undefined);
  r = E(t, diffItem({ oldText: 'x\ny\n', newText: null })); assert.equal(r.some, false);
  // missing oldText without a reliable "Created" message is not a creation
  r = E(t, diffItem({ newText: 'x\ny\n' })); assert.equal(r.some, false);
  r = E(t, diffItem({ oldText: null, newText: 'x\ny\n' })); assert.equal(r.some, false);
  // missing oldText + explicit Created + newText: a new file
  r = E(t, diffItem({ newText: 'x\ny\nz\n' }, 'Created the /p/a file.')); assert.deepEqual([r.exact, r.added, r.deleted, r.files[0].created], [true, 3, 0, true]);
  // Created message but no newText: nothing is invented
  r = E(t, diffItem({}, 'Created the /p/a file.')); assert.equal(r.some, false);
  // failed tools count nothing
  r = E(t, { ...diffItem({ oldText: 'a', newText: 'b' }), status: 'failed' }); assert.equal(r.some, undefined); assert.equal(r.added, 0);
});

test('patch parsing: only real hunk lines; incomplete or unprovable records stay unknown', async () => {
  const t = await boot(newWorld({ c1: conv('c1', []) })).ready();
  const known = f => [f.known, f.added, f.deleted];
  // unified diff: content that looks like headers inside a hunk is content
  let r = P(t, 'diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n@@ -1,3 +1,3 @@\n keep\n--- old dashes\n+++ new pluses\n tail\n');
  assert.equal(r.length, 1); assert.equal(r[0].path, 'x.txt'); assert.deepEqual(known(r[0]), [true, 1, 1]);
  // pure ---/+++ multi-file without diff --git, second file named like a header line
  r = P(t, '--- a/one.txt\n+++ b/one.txt\n@@ -1 +1,2 @@\n a\n+b\n--- a/two.txt\n+++ b/two.txt\n@@ -1,2 +1 @@\n a\n-b\n');
  assert.deepEqual(r.map(f => [f.path, ...known(f)]), [['one.txt', true, 1, 0], ['two.txt', true, 0, 1]]);
  // new / deleted file via /dev/null
  r = P(t, '--- /dev/null\n+++ b/n.txt\n@@ -0,0 +1,2 @@\n+a\n+b\n--- a/d.txt\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n');
  assert.deepEqual(r.map(f => [f.path, ...known(f)]), [['n.txt', true, 2, 0], ['d.txt', true, 0, 2]]);
  // no hunk, binary, rename-only: unknown, no numbers
  for (const text of ['diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n',
    'diff --git a/b.bin b/b.bin\nBinary files a/b.bin and b/b.bin differ\n',
    'diff --git a/o b/n\nsimilarity index 100%\nrename from o\nrename to n\n',
    'diff --git a/m b/m\nold mode 100644\nnew mode 100755\n']) {
    const files = P(t, text); assert.equal(files.length, 1); assert.deepEqual(known(files[0]), [false, null, null]);
  }
  // truncated hunk (declares 5 old lines, only 2 present): unknown, not a short count
  r = P(t, '--- a/t.txt\n+++ b/t.txt\n@@ -1,5 +1,5 @@\n a\n-b\n'); assert.deepEqual(known(r[0]), [false, null, null]);
  // a good file followed by a truncated one: only the truncated one is unknown
  r = P(t, '--- a/g.txt\n+++ b/g.txt\n@@ -1 +1 @@\n-a\n+b\n--- a/t.txt\n+++ b/t.txt\n@@ -1,4 +1,4 @@\n-a\n');
  assert.deepEqual(r.map(f => f.known), [true, false]);
  // "\ No newline" is not a line
  r = P(t, '--- a/n\n+++ b/n\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n\\ No newline at end of file\n'); assert.deepEqual(known(r[0]), [true, 1, 1]);
  // apply_patch: Add File needs no @@; Delete File with only a path is unknown; missing End Patch is unknown
  r = P(t, '*** Begin Patch\n*** Add File: a.py\n+x\n+y\n*** Delete File: gone.py\n*** Update File: u.py\n@@\n-o\n+n\n+n2\n*** End Patch');
  assert.deepEqual(r.map(f => [f.path, ...known(f)]), [['a.py', true, 2, 0], ['gone.py', false, null, null], ['u.py', true, 2, 1]]);
  r = P(t, '*** Begin Patch\n*** Update File: u.py\n@@\n-o\n+n\n'); assert.deepEqual(known(r[0]), [false, null, null]);
  r = P(t, '*** Begin Patch\n*** Update File: u.py\n*** Move to: v.py\n*** End Patch'); assert.deepEqual([r[0].path, ...known(r[0])], ['v.py', false, null, null]);
  // an added line whose text starts with "++ " or "-- " is counted as content
  r = P(t, '*** Begin Patch\n*** Add File: h.txt\n+++ not a header\n+--- also content\n*** End Patch'); assert.deepEqual(known(r[0]), [true, 2, 0]);
  assert.equal(P(t, ''), null); assert.equal(P(t, 'just text'), null);
});

test('patch tools: unknown files add nothing, partial application output is conservative', async () => {
  const t = await boot(newWorld({ c1: conv('c1', []) })).ready();
  const patch = '*** Begin Patch\n*** Update File: a.py\n@@\n-o\n+n\n*** Delete File: gone.py\n*** End Patch';
  let r = E(t, { title: 'Apply Patch', kind: 'edit', rawInput: { patch }, rawOutput: { message: 'Done' } });
  assert.deepEqual(r.files.map(f => [f.path, f.exact]), [['a.py', true], ['gone.py', false]]);
  assert.equal(r.exact, false); assert.equal(r.some, true); assert.deepEqual([r.added, r.deleted], [1, 1]);   // delete-file is never "-0"
  assert.match(r.files[1].note, /未记录/);
  for (const message of ['Patch partially applied', 'Hunk #2 FAILED at 10', 'applied 1 of 2 hunks; rejected 1', '部分应用成功']) {
    r = E(t, { title: 'Apply Patch', kind: 'edit', rawInput: { patch }, rawOutput: { message } });
    assert.equal(r.some, false, message); assert.equal(r.added, 0, message);
    assert.match(r.files[0].note, /部分应用/);
  }
  r = E(t, { title: 'Apply Patch', kind: 'edit', rawInput: { patch: 'diff --git a/x b/x\nBinary files a/x and b/x differ\n' }, rawOutput: { message: 'ok' } });
  assert.equal(r.some, false);
});

test('an existing specific Chinese title is kept; generic ones fall back to the category; shell is not guessed', async () => {
  const t = await boot(newWorld({ c1: conv('c1', []) })).ready();
  const T = tool => t.ev('toolSummary(' + JSON.stringify(tool) + ')');
  assert.equal(T({ title: '修改配置文件 config.yaml', kind: 'edit', rawInput: { path: '/p/config.yaml' } }).title, '修改配置文件 config.yaml');
  assert.equal(T({ title: '读取 README 说明', kind: 'read', rawInput: {} }).title, '读取 README 说明');
  assert.equal(T({ title: '检索项目中的登录逻辑', kind: 'search', rawInput: {} }).title, '检索项目中的登录逻辑');
  assert.equal(T({ title: '删除旧的构建产物', kind: 'delete', rawInput: {} }).title, '删除旧的构建产物');
  assert.equal(T({ title: '修改文件', kind: 'edit', rawInput: {} }).title, '修改文件');
  assert.equal(T({ title: '工具调用', kind: 'search', rawInput: {} }).title, '搜索内容');
  // unknown shell keeps the accurate Chinese summary; the command text is never interpreted
  const sh = T({ title: '运行单元测试并汇总失败用例', kind: 'execute', rawInput: { command: 'mytool --go' } });
  assert.equal(sh.title, '运行单元测试并汇总失败用例'); assert.equal(sh.unknown, true); assert.equal(sh.actions.length, 0);
  assert.equal(T({ title: '执行命令', kind: 'execute', rawInput: { command: 'mytool --go' } }).title, '执行命令');
  assert.equal(T({ title: 'Run Command', kind: 'execute', rawInput: { command: 'mytool --go' } }).title, '执行命令');
  // a kept title does not hide that the command may modify files
  assert.equal(T({ title: '整理一下目录', kind: 'execute', rawInput: { command: 'rm -rf build' } }).mutates, true);
  // rendered card: title kept, note does not claim "执行命令"
  const events = cmd('k1', 'mytool --go').map(l => l.replace(/"title":"Run Command"/g, '"title":"运行单元测试并汇总失败用例"'));
  const page = await boot(newWorld({ c1: conv('c1', events) })).ready();
  assert.equal(page.title(0), '运行单元测试并汇总失败用例');
  assert.match(page.card(0).textContent, /沿用工具给出的中文描述/);
  assert.doesNotMatch(page.card(0).textContent, /按“执行命令”显示/);
});

test('Claude continuation renders current messages and tool output without duplicating the final result', async () => {
  const c = conv('claude', [
    JSON.stringify({sessionUpdate:'agent_message_chunk',content:{type:'text',text:'本轮 Claude 回复'}}),
    JSON.stringify({sessionUpdate:'tool_call',toolCallId:'read-1',title:'Read',kind:'read',status:'in_progress',rawInput:{file_path:'/p/a.txt'}}),
    JSON.stringify({sessionUpdate:'tool_call_update',toolCallId:'read-1',status:'completed',rawOutput:'内容'}),
    JSON.stringify({type:'runFinished',data:{finalText:'本轮 Claude 回复',status:'success'}})
  ]);
  c.runs[0].state.provider='claude';c.runs[0].state.resume_id='native-session';
  const t = await boot(newWorld({claude:c})).ready();await t.openConv('claude');
  assert.equal(t.cards().length,1);
  assert.equal(t.$('conversation').textContent.split('本轮 Claude 回复').length-1,1);
  assert.match(t.$('conversation').textContent,/Claude/);
  assert.doesNotMatch(t.$('conversation').textContent,/Kiro/);
  assert.ok(t.$('provider-select'));
  const usage=t.ev('usageModel({status:"unsupported",message:"Claude 暂不支持账户额度"})');
  assert.equal(usage.main,'Claude 暂不支持账户额度');
});

test('settings categories keep installation with CLI and load storage only on demand', async () => {
  const w=newWorld({});w.connections={providers:[{id:'claude',name:'Claude Code',path:'',status:'disconnected',available:false}],default_provider:null};
  const t=await boot(w).ready();t.click(t.$('settings-open'));await settle();
  assert.equal(t.$('settings-page-cli').hidden,false);
  assert.equal(t.$('settings-page-storage').hidden,true);
  assert.equal(t.calls.filter(c=>c.path==='/api/temp/overview').length,0);
  t.click(t.$('cli-card-open-claude'));await settle();
  assert.ok(t.$('cli-config-claude').contains(t.$('cli-install-claude')));
  t.$('cli-path-claude').value='/typed/claude';t.$('cli-path-claude').dispatchEvent(new t.window.Event('input'));
  t.click(t.$('settings-tab-storage'));await settle();
  assert.equal(t.$('settings-tab-storage').getAttribute('aria-selected'),'true');
  assert.equal(t.$('settings-page-cli').hidden,true);
  assert.ok(t.$('settings-page-storage').contains(t.$('retention-note')));
  assert.ok(t.$('settings-page-storage').contains(t.$('log-root')));
  assert.equal(t.calls.filter(c=>c.path==='/api/temp/overview').length,1);
  t.click(t.$('settings-tab-service'));
  assert.ok(t.$('settings-page-service').contains(t.$('stop-viewer')));
  t.click(t.$('settings-tab-cli'));await settle();
  assert.equal(t.$('cli-path-claude').value,'/typed/claude');
  assert.equal(t.calls.filter(c=>c.post).length,0);
  assert.equal(t.errors.length,0);
});

test('settings tabs support keyboard navigation and preserve the active category on reopen', async () => {
  const t=await boot(newWorld({})).ready();t.click(t.$('settings-open'));
  const key=(id,value)=>t.$(id).dispatchEvent(new t.window.KeyboardEvent('keydown',{key:value,bubbles:true}));
  key('settings-tab-cli','ArrowDown');
  assert.equal(t.document.activeElement.id,'settings-tab-skill');
  key('settings-tab-skill','ArrowDown');
  assert.equal(t.document.activeElement.id,'settings-tab-roles');
  key('settings-tab-roles','ArrowDown');
  assert.equal(t.document.activeElement.id,'settings-tab-storage');
  assert.equal(t.$('settings-page-storage').hidden,false);
  key('settings-tab-storage','End');
  assert.equal(t.document.activeElement.id,'settings-tab-help');
  assert.equal(t.$('settings-page-help').hidden,false);
  t.click(t.$('settings-close'));t.click(t.$('settings-open'));
  assert.equal(t.$('settings-page-help').hidden,false);
  key('settings-tab-help','Home');
  assert.equal(t.document.activeElement.id,'settings-tab-cli');
  assert.equal(t.$('settings-tab-cli').tabIndex,0);
  assert.equal(t.$('settings-tab-help').tabIndex,-1);
});

test('narrow settings use horizontal tab semantics', async () => {
  const t=await boot(newWorld({}),{width:390}).ready();
  assert.equal(t.$('settings-nav').getAttribute('aria-orientation'),'horizontal');
  t.click(t.$('settings-open'));t.key('ArrowRight',t.$('settings-tab-cli'));
  assert.equal(t.$('settings-page-skill').hidden,false);
});

test('GitHub settings check only when opened and show only the active device code', async () => {
  const w = newWorld({});
  w.githubAuth = action => respond(action==='check'?{state:'signed_out'}:
    action==='login'?{state:'waiting',code:'ABCD-1234'}:{state:'connected',login:'demo-user'});
  const t = await boot(w).ready();
  t.click(t.$('settings-open'));
  assert.equal(t.calls.some(call=>call.path==='/api/github/auth'),false);
  t.click(t.$('settings-tab-github')); await settle();
  assert.equal(t.$('github-login').hidden,false);
  t.click(t.$('github-login')); await settle();
  assert.equal(t.$('github-device').hidden,false);
  assert.equal(t.$('github-device').querySelector('a').href,'https://github.com/login/device');
  t.click(t.$('github-copy-code')); await settle();
  assert.equal(t.copied.at(-1),'ABCD-1234');
  await t.ev('githubAuth()');
  assert.match(t.$('github-status').textContent,/demo-user/);
  assert.equal(t.$('github-device').hidden,true);
  assert.equal(t.$('github-code').textContent,'');
  assert.deepEqual(t.errors,[]);
});

test('CLI cards group connections and pause without opening configuration', async () => {
  const w=newWorld({});w.connections={default_provider:'claude',providers:[{id:'claude',name:'Claude Code',connected:true,available:true,status:'connected',path:'/saved/claude'},
    {id:'kiro',name:'Kiro CLI',connected:false,available:false,status:'disconnected',path:''}]};
  w.connectionAction=body=>{assert.equal(body.provider,'claude');assert.ok(['pause','start'].includes(body.action));
    Object.assign(w.connections.providers[0],{paused:body.action==='pause',available:body.action==='start',status:body.action==='pause'?'paused':'connected'});
    return respond({ok:true,message:body.action==='pause'?'已暂停':'已启动',...w.connections});};
  const t=await boot(w).ready();t.click(t.$('settings-open'));await settle();
  assert.ok(t.$('cli-connected-list').contains(t.$('cli-card-claude')));
  assert.ok(t.$('cli-disconnected-list').contains(t.$('cli-card-kiro')));
  assert.ok(t.$('cli-card-claude').querySelector('svg,img'));
  assert.ok(t.$('cli-card-kiro').querySelector('svg,img'));
  t.click(t.$('cli-toggle-claude'));await settle();
  assert.equal(t.$('cli-overview').hidden,false);
  assert.ok(t.$('cli-connected-list').contains(t.$('cli-card-claude')));
  assert.equal(t.$('cli-toggle-claude').textContent,'启动');
  t.click(t.$('cli-toggle-claude'));await settle();
  assert.equal(t.$('cli-toggle-claude').textContent,'暂停');
  t.click(t.$('cli-card-open-claude'));await settle();
  assert.equal(t.$('cli-connect-claude').textContent,'暂停');
  t.click(t.$('cli-connect-claude'));await settle();
  assert.equal(t.$('cli-connect-claude').textContent,'启动');
  assert.equal(t.$('cli-toggle-claude').textContent,'启动');
  t.click(t.$('cli-connect-claude'));await settle();
  assert.equal(t.$('cli-connect-claude').textContent,'暂停');
  assert.equal(t.$('cli-toggle-claude').textContent,'暂停');
  assert.equal(t.calls.filter(c=>c.body?.action==='disconnect').length,0);
  assert.equal(t.calls.filter(c=>c.body?.action==='connect').length,0);
  assert.equal(t.errors.length,0);
});

test('provider configuration displays model lists for a non-default CLI and retains errors safely', async () => {
  const w=newWorld({});w.connections={default_provider:'claude',providers:[{id:'claude',name:'Claude Code',connected:true,available:true,status:'connected'},
    {id:'kiro',name:'Kiro CLI',connected:true,available:true,status:'connected',path:'/saved/kiro'}]};
  let fail=false;
  w.connectionAction=body=>{assert.equal(body.provider,'kiro');assert.equal(body.action,'catalog');
    return fail?respond({ok:false,message:'读取目录失败'},400):respond({ok:true,message:'目录已读取',catalog:{status:'ok',models:[{id:'model-one',name:'First',effort:{levels:['low','high']}},{id:'<script>danger</script>',name:'<img onerror=oops>'}]}});};
  const t=await boot(w).ready();t.click(t.$('settings-open'));await settle();
  t.click(t.$('cli-card-open-kiro'));await settle();
  assert.equal(t.$('cli-overview').hidden,true);
  assert.equal(t.$('cli-config-kiro').hidden,false);
  assert.equal(t.$('cli-config-claude').hidden,true);
  assert.equal(t.$('cli-path-kiro').value,'/saved/kiro');
  assert.ok(t.$('cli-install-kiro').textContent.includes('kiro-cli'));
  t.click(t.$('cli-catalog-kiro'));await settle();
  assert.ok(t.$('cli-model-list-kiro').textContent.includes('model-one'));
  assert.ok(t.$('cli-model-list-kiro').textContent.includes('low / high'));
  assert.equal(t.$('cli-model-list-kiro').querySelector('script,img'),null);
  fail=true;t.click(t.$('cli-catalog-kiro'));await settle();
  assert.match(t.$('cli-feedback-kiro').textContent,/读取目录失败/);
  assert.ok(t.$('cli-model-list-kiro').textContent.includes('model-one'));
  t.click(t.$('cli-config-back'));assert.equal(t.$('cli-overview').hidden,false);
  assert.equal(t.document.activeElement.id,'cli-card-open-kiro');
  assert.equal(t.errors.length,0);
});

test('switching the default CLI updates the overview and new drafts, not an existing conversation', async () => {
  const c=conv('claude',[]);c.provider='claude';
  const w=newWorld({claude:c});w.connections={default_provider:'claude',providers:[
    {id:'claude',name:'Claude Code',connected:true,available:true,status:'connected'},
    {id:'kiro',name:'Kiro CLI',connected:true,available:true,status:'connected'}]};
  w.connectionAction=body=>{
    assert.equal(body.action,'default');assert.equal(body.provider,'kiro');
    w.connections.default_provider='kiro';return respond({ok:true,...w.connections});
  };
  const t=await boot(w).ready();await t.openConv('claude');
  t.click(t.$('settings-open'));await settle();
  assert.equal(t.$('cli-default-badge-claude').hidden,false);
  t.$('cli-default-kiro').click();await settle();
  assert.equal(t.$('cli-overview').hidden,false);
  assert.equal(t.$('cli-default-badge-claude').hidden,true);
  assert.equal(t.$('cli-default-badge-kiro').hidden,false);
  assert.equal(t.$('provider-select').value,'claude');
  t.ev("select({group:'other'})");await settle();
  const picker=t.$('provider-select');
  assert.equal(picker.value,'kiro');
  picker.value='claude';picker.dispatchEvent(new t.window.Event('change'));await t.poll();
  assert.equal(picker.value,'claude');
});

test('new user connects Claude from settings without automatic model queries', async () => {
  const w=newWorld({});w.connections={default_provider:null,providers:[{id:'claude',name:'Claude Code',path:'',status:'disconnected',available:false},{id:'kiro',name:'Kiro CLI',path:'',status:'disconnected',available:false}]};
  w.connectionAction=body=>{
    assert.equal(body.action,'connect');assert.equal(body.provider,'claude');assert.equal(body.path,'/custom/claude');
    w.connections.default_provider='claude';Object.assign(w.connections.providers[0],{available:true,connected:true,path:body.path,status:'connected',version:'2.1.78 (Claude Code)'});
    return respond({ok:true,message:'连接成功',...w.connections});
  };
  const t=await boot(w).ready();
  assert.equal(t.calls.filter(c=>c.path==='/api/models').length,0);
  assert.match(t.$('composer-hint').textContent,/连接/);
  t.click(t.$('settings-open'));await settle();
  assert.equal(t.calls.filter(c=>c.post).length,0);
  t.click(t.$('cli-card-open-claude'));await settle();
  t.$('cli-path-claude').value='/custom/claude';t.$('cli-path-claude').dispatchEvent(new t.window.Event('input'));
  t.click(t.$('cli-connect-claude'));await settle();
  assert.match(t.$('cli-feedback-claude').textContent,/连接成功/);
  assert.equal(t.$('cli-path-claude').value,'/custom/claude');
  assert.equal(t.$('cli-model-card').hidden,false);
  assert.equal(t.$('cli-model-dialog').open,true);
  assert.equal(t.$('cli-model-form').closest('dialog'),t.$('cli-model-dialog'));
  t.$('cli-model-dialog').close();await settle();
  assert.equal(t.$('cli-model-dialog').open,false);
  assert.equal(t.$('cli-model-form').parentElement,t.$('cli-model-card'));
  assert.equal(t.$('cli-test-claude').disabled,false);
  assert.equal(t.$('cli-default-badge-claude').hidden,false);
  assert.equal(t.$('provider-select').value,'claude');
  assert.equal(t.calls.filter(c=>c.body?.action==='test').length,0);
  assert.equal(t.errors.length,0);
});

test('Kiro connect applies returned catalog without another request or paid test', async () => {
  const w=newWorld({});w.connections={default_provider:'claude',providers:[
    {id:'kiro',name:'Kiro CLI',path:'',status:'disconnected',available:false},
    {id:'claude',name:'Claude Code',connected:true,available:true,status:'connected',defaults_configured:true,defaults:{model:'previous-claude',effort:'high'}}]};
  w.roles={revision:'r1',roles:[
    {id:'architect',name:'架构师',provider:'claude',model:null,effort:null},
    {id:'coder',name:'编码',provider:null,model:null,effort:null}]};
  const catalog={status:'ok',models:[{id:'first',name:'First',selectable:true,effort:{known:true,supported:false,levels:[]}},
    {id:'second',name:'Second',selectable:true,effort:{known:true,supported:false,levels:[]}}],default:{available:true,model:'first',effort:null}};
  w.connectionAction=body=>{
    if(body.action==='defaults'){
      assert.equal(body.provider,'kiro');assert.equal(body.model,'second');assert.equal(body.effort,null);
      assert.deepEqual(body.overrides,{});assert.deepEqual(body.switch_roles,[]);
      Object.assign(w.connections.providers[0],{defaults_configured:true,defaults:{model:'second',effort:null}});
      return respond({ok:true,...w.connections});
    }
    assert.equal(body.action,'connect');
    Object.assign(w.connections.providers[0],{available:true,connected:true,path:'/saved/kiro',status:'connected'});
    return respond({ok:true,message:'连接成功，模型目录已读取',catalog,...w.connections});
  };
  const t=await boot(w).ready();t.click(t.$('settings-open'));await settle();
  t.click(t.$('cli-card-open-kiro'));await settle();
  t.click(t.$('cli-connect-kiro'));await settle();
  assert.equal(t.ev('cliCatalogs.kiro.models.length'),2);
  assert.equal(t.$('provider-select').value,'claude');
  assert.equal(t.calls.filter(c=>c.path==='/api/models'&&c.query.refresh).length,0);
  assert.equal(t.calls.filter(c=>c.body?.action==='test').length,0);
  assert.match(t.$('cli-feedback-kiro').textContent,/目录已读取/);
  assert.equal(t.$('cli-model-card').hidden,false);
  assert.equal(t.$('cli-model-dialog').open,true);
  const architect=t.$('cli-model-select-role-architect'),coder=t.$('cli-model-select-role-coder');
  assert.equal(architect.value,'@other');
  assert.match(architect.selectedOptions[0].textContent,/保留 Claude Code · previous-claude/);
  assert.equal(t.$('cli-effort-select-role-architect').selectedOptions[0].textContent,'高');
  assert.equal(coder.value,'@other');
  assert.equal(coder.selectedOptions[0].textContent,'保留 Claude Code · previous-claude');
  assert.equal(t.$('cli-effort-select-role-coder').selectedOptions[0].textContent,'高');
  assert.equal(t.$('cli-model-select-default').value,'');
  assert.equal(t.$('cli-model-save').disabled,true);
  t.$('cli-model-select-default').value='second';
  t.$('cli-model-select-default').dispatchEvent(new t.window.Event('change'));
  assert.equal(architect.value,'@other');
  assert.equal(coder.value,'@other');
  assert.equal(coder.selectedOptions[0].textContent,'保留 Claude Code · previous-claude');
  assert.equal(t.$('cli-model-save').disabled,false);
  assert.equal(t.$('cli-effort-select-default').disabled,true);
  t.$('cli-model-save').click();await settle();
  assert.equal(t.$('cli-model-dialog').open,false);
  assert.equal(t.$('cli-model-form').parentElement,t.$('cli-model-card'));
  assert.equal(t.$('cli-model-select-default').value,'second');
});

test('connection failures retain typed paths and test call is explicit', async () => {
  const w=newWorld({});w.connections={default_provider:'claude',providers:[{id:'claude',name:'Claude Code',path:'/saved/claude',status:'connected',connected:true,available:true}]};
  w.connectionAction=body=>body.action==='test'?respond({ok:true,message:'模型测试成功'}):respond({ok:false,message:'尚未登录，请执行 claude auth login'},400);
  const t=await boot(w).ready();t.click(t.$('settings-open'));await settle();
  t.click(t.$('cli-card-open-claude'));await settle();
  t.$('cli-path-claude').value='/typed/claude';t.$('cli-path-claude').dispatchEvent(new t.window.Event('input'));
  await t.poll();assert.equal(t.$('cli-path-claude').value,'/typed/claude');
  t.click(t.$('cli-connect-claude'));await settle();
  assert.match(t.$('cli-feedback-claude').textContent,/尚未登录/);
  assert.equal(t.$('cli-path-claude').value,'/typed/claude');
  t.click(t.$('cli-test-claude'));await settle();
  assert.match(t.$('cli-feedback-claude').textContent,/模型测试成功/);
  assert.equal(t.calls.filter(c=>c.body?.action==='test').length,1);
});

test('new conversations list only connected CLIs and wait for an explicit choice', async () => {
  const w=newWorld({});w.providers=[{id:'kiro',name:'Kiro CLI',available:false},{id:'claude',name:'Claude Code',available:true}];
  const t=await boot(w).ready();
  const picker=t.$('provider-select');
  assert.equal(picker.value,'');
  assert.deepEqual([...picker.options].map(o=>o.value),['','claude']);
  assert.equal(t.calls.some(c=>c.path==='/api/models'),false);
  picker.value='claude';picker.dispatchEvent(new t.window.Event('change'));await settle();
  const requests=t.calls.filter(c=>c.path==='/api/models');
  assert.ok(requests.length>0);
  assert.ok(requests.every(c=>c.query.provider==='claude'),JSON.stringify(requests));
  w.providers[1].available=false;await t.poll();
  assert.equal(picker.value,'');assert.equal(picker.disabled,true);
  assert.deepEqual([...picker.options].map(o=>o.value),['']);
});

test('provider selector routes model requests and locks existing Claude conversations', async () => {
  const c=conv('claude',[]);c.provider='claude';c.config={model:'sonnet',effort:'medium'};
  const w=newWorld({claude:c});w.providers=[{id:'kiro',name:'Kiro CLI',available:true},{id:'claude',name:'Claude Code',available:true}];w.default_provider='kiro';
  w.models=q=>({provider:q.get('provider'),status:'ok',models:[{id:'sonnet',name:'Sonnet',selectable:true,effort:{known:true,supported:true,levels:['medium'],default:'medium'}}],default:{available:true,model:'sonnet',effort:'medium'}});
  const t=await boot(w).ready();await t.openConv('claude');
  assert.equal(t.$('provider-select').value,'claude');assert.equal(t.$('provider-select').disabled,true);
  assert.ok(t.calls.some(c=>c.path==='/api/models'&&c.query.provider==='claude'));
  t.ev("select({group:'g'})");await settle();
  assert.equal(t.$('provider-select').disabled,false);
  t.$('provider-select').value='claude';t.$('provider-select').dispatchEvent(new t.window.Event('change'));await settle();
  assert.equal(t.$('model-select').value,'sonnet');
  assert.equal(t.ev('currentProvider()'),'claude');
  assert.match(t.$('account').textContent,/刷新|额度/);
  assert.equal(t.errors.length,0);
});

test('composer selects role defaults and sends explicit per-conversation overrides', async () => {
  const w=newWorld({});w.default_provider='kiro';
  w.providers=[{id:'kiro',name:'Kiro CLI',available:true},{id:'claude',name:'Claude Code',available:true,defaults_configured:true,defaults:{model:'one',effort:'high'}}];
  w.roles={roles:[{id:'coder',name:'编码',enabled:true,provider:'claude',model:null,effort:null},
    {id:'disabled',name:'停用',enabled:false,provider:'kiro'}]};
  w.models=q=>{const ids=q.get('provider')==='claude'?['one']:['two','three'];return {
    status:'ok',models:ids.map(id=>({id,name:id,selectable:true,effort:{known:true,supported:true,levels:['low','high'],default:'high'}})),
    default:{model:ids[0],effort:'high',available:true}
  }};
  w.send=()=>respond({error:'mocked send'},409);
  const t=await boot(w).ready();
  const change=(id,value)=>{const node=t.$(id);node.value=value;node.dispatchEvent(new t.window.Event('change'))};
  const submit=async()=>{t.$('composer-form').dispatchEvent(new t.window.Event('submit',{cancelable:true}));await settle();return t.calls.filter(c=>c.path==='/api/send').at(-1)?.body};
  assert.equal(t.$('permission-select'),null);
  assert.deepEqual([...t.$('composer-role').options].map(o=>o.value),['','coder']);
  change('composer-role','coder');await settle();
  assert.equal(t.$('provider-select').value,'claude');
  assert.equal(t.$('model-select').value,'one');
  assert.equal(t.$('effort-select').value,'high');
  assert.equal(t.$('provider-select').disabled,false);
  assert.equal(t.$('effort-select').disabled,false);
  t.$('prompt').value='实现功能';
  const base={group:'g',source:null,agent:null,text:'实现功能',role_id:'coder'};
  assert.deepEqual(await submit(),base);
  change('provider-select','kiro');await settle();
  change('model-select','three');change('effort-select','low');
  assert.deepEqual(await submit(),{...base,provider:'kiro',model:'three',effort:'low'});
  assert.equal(t.$('composer-role').value,'coder');
});

test('a completed Claude turn shows tool denial separately from its status', async () => {
  const c=conv('denied',[]);c.provider='claude';
  c.runs[0].summary={provider:'claude',result:'completed',exit_code:0,stop_reason:'success',permission_denial_count:1,task_seconds:4};
  const t=await boot(newWorld({denied:c})).ready();await t.openConv('denied');
  const summary=t.document.querySelector('.turn-summary');
  assert.match(summary.textContent,/已完成/);
  assert.equal(summary.querySelector('.warn').textContent,'工具调用被拒绝 1 次');
  assert.doesNotMatch(summary.textContent,/执行失败|停止原因|退出码/);
});

test('Claude API quota uses actual units and optional reset without composer model refresh', async () => {
  const t=await boot(newWorld({})).ready();
  assert.equal(t.$('models-refresh'),null);
  const result=t.ev('usageModel({provider:"claude",status:"ok",fetched_at:"2026-11-20T02:17:44Z",data:{used:400,limit:500,unit:"USD",period:"daily",next_reset_at:"2026-11-20T05:37:44Z"}}, Date.parse("2026-11-20T02:17:44Z"))');
  assert.match(result.main,/400.*used \/ 500/);assert.match(result.main,/USD/);
  assert.equal(result.reset,'3h 20m 后重置');assert.equal(result.coverage,null);
  const noReset=t.ev('usageModel({provider:"claude",status:"ok",data:{used:0,limit:500,unit:"USD",period:"daily"}})');
  assert.equal(noReset.reset,'重置时间未知');assert.doesNotMatch(noReset.reset,/Kiro/);
  const expired=t.ev('usageModel({provider:"claude",status:"ok",data:{used:400,limit:500,next_reset_at:"2026-11-20T02:00:00Z"}}, Date.parse("2026-11-20T02:17:44Z"))');
  assert.equal(expired.reset,'等待重置');
});

test('global skill setup copies a portable prompt without exposing the local source path', async () => {
  const w=newWorld({});w.installationRoot='/Users/example/My Agents/<checkout>';
  const t=await boot(w).ready();t.click(t.$('settings-open'));t.click(t.$('settings-tab-skill'));await settle();
  assert.equal(t.$('settings-page-skill').hidden,false);
  const prompt=t.$('skill-prompt').value;
  assert.equal(prompt.includes(w.installationRoot),false);assert.match(prompt,/当前对话的工作目录/);assert.match(prompt,/全局/);
  assert.equal(t.$('skill-setup-instruction').parentElement.textContent,'在此项目中与codex对话，发送以下提示词，配置全局skill。');
  assert.equal(t.$('skill-source'),null);
  assert.doesNotMatch(prompt,/CODEX_HOME|~\/\.codex\/skills|~\/\.agents\/skills/);
  assert.match(prompt,/自行.*全局.*目录/);
  assert.ok(prompt.includes('integrations/codex/SKILL.md.template'));
  assert.ok(prompt.includes('integrations/codex/openai.yaml.template'));
  assert.doesNotMatch(prompt,/roles list|run --role|state\.json/);
  t.click(t.$('skill-copy'));await settle();assert.equal(t.copied.pop(),prompt);
  assert.equal(t.calls.some(c=>c.post),false);assert.equal(t.errors.length,0);
  w.installationRoot=null;t.ev('refresh()');await settle();
  assert.equal(t.$('skill-copy').disabled,false);assert.equal(t.$('skill-prompt').value,prompt);
});

test('global roles support custom edits, toggles, delete and preserve drafts on errors and tab changes', async () => {
  const role={id:'coder',name:'编码',enabled:true,description:'实现业务代码',provider:'claude',model:'sonnet',effort:'medium',instructions:'按任务实现',constraints:['不修改测试'],deliverables:['改动说明']};
  const w=newWorld({});w.roles={version:1,revision:'r1',roles:[role,{...role,id:'tester',name:'测试'}]};
  w.models=()=>({status:'ok',models:[{id:'sonnet',name:'Sonnet',selectable:true,effort:{known:true,supported:true,levels:['low','medium','high'],default:'medium'}}],default:{model:'sonnet',effort:'medium'}});
  w.roleAction=body=>{
    assert.equal(body.revision,w.roles.revision);
    if(body.role?.name==='失败')return respond({error:'配置已更新，请刷新'},400);
    if(body.action==='delete')w.roles.roles=w.roles.roles.filter(r=>r.id!==body.id);
    else {const index=w.roles.roles.findIndex(r=>r.id===body.role.id);if(index<0)w.roles.roles.push(body.role);else w.roles.roles[index]=body.role;}
    w.roles.revision+='x';return respond(w.roles);
  };
  const t=await boot(w).ready();assert.equal(t.calls.some(c=>c.path==='/api/roles'),false);
  t.click(t.$('settings-open'));t.click(t.$('settings-tab-roles'));await settle();
  assert.match(t.$('roles-list').textContent,/编码/);assert.match(t.$('roles-list').textContent,/测试/);
  t.click(t.$('role-open-coder'));
  assert.equal(t.$('roles-overview').hidden,true);
  assert.equal(t.$('role-detail').hidden,false);
  assert.equal(t.document.activeElement,t.$('role-detail-back'));
  t.click(t.$('role-detail-back'));
  assert.equal(t.$('role-detail').hidden,true);
  assert.equal(t.document.activeElement,t.$('role-open-coder'));
  t.click(t.$('role-new'));t.$('role-id').value='docs';t.$('role-name').value='文档';
  await settle();
  assert.equal(t.$('role-provider').value,'');
  assert.equal(t.$('role-model').disabled,true);
  t.$('role-description').value='维护文档';t.$('role-instructions').value='按任务更新文档';
  const items=key=>[...t.$('role-'+key).querySelectorAll('textarea')];
  for(const [key,values] of Object.entries({constraints:['保留格式\n说明','删除这项','  '],deliverables:[' 更新说明 ']})){
    for(const value of values){
      t.click(t.$('role-'+key+'-add'));
      const item=items(key).at(-1);assert.equal(t.document.activeElement,item);item.value=value;
    }
  }
  t.click(items('constraints')[1].nextElementSibling);
  t.click(t.$('settings-tab-service'));t.click(t.$('settings-tab-roles'));await settle();assert.equal(t.$('role-name').value,'文档');
  assert.equal(t.$('role-enabled').form,t.$('role-form'));
  t.$('role-save').click();await settle();
  assert.equal(t.$('roles-overview').hidden,false);
  assert.equal(w.roles.roles.find(r=>r.id==='docs').name,'文档');
  assert.equal(w.roles.roles.find(r=>r.id==='docs').provider,null);
  assert.deepEqual(w.roles.roles.find(r=>r.id==='docs').constraints,['保留格式\n说明']);
  assert.deepEqual(w.roles.roles.find(r=>r.id==='docs').deliverables,['更新说明']);
  t.click(t.$('role-toggle-docs'));await settle();assert.equal(w.roles.roles.find(r=>r.id==='docs').enabled,false);
  t.click(t.$('role-open-docs'));await settle();assert.equal(t.$('role-id').disabled,true);t.$('role-name').value='失败';
  assert.equal(items('constraints')[0].value,'保留格式\n说明');
  t.$('role-form').dispatchEvent(new t.window.Event('submit',{bubbles:true,cancelable:true}));await settle();
  assert.match(t.$('roles-feedback').textContent,/配置已更新/);assert.equal(t.$('role-name').value,'失败');
  t.click(t.$('role-delete'));await settle();assert.equal(w.roles.roles.some(r=>r.id==='docs'),false);
  t.click(t.$('role-open-coder'));assert.equal(t.$('role-delete').hidden,true);
  assert.equal(t.calls.some(c=>c.path==='/api/models'&&(c.post||c.query.refresh)),false);assert.equal(t.errors.length,0);
});

test('role model selects follow provider catalogs and effort, reject missing models and ignore stale responses', async () => {
  const role={id:'coder',name:'编码',enabled:true,description:'编码',provider:'claude',model:'sonnet',effort:'medium',instructions:'实现',constraints:[],deliverables:[]};
  const w=newWorld({});w.roles={version:1,revision:'r',roles:[role]};
  const claude={status:'ok',models:[{id:'sonnet',name:'Sonnet',selectable:true,effort:{known:true,supported:true,levels:['low','medium','high'],default:'medium'}},{id:'haiku',name:'Haiku',selectable:true,effort:{known:true,supported:false,levels:[],default:null}}],default:{model:'sonnet',effort:'medium'}};
  const kiro={status:'ok',models:[{id:'kiro-model',name:'Kiro Model',selectable:true,effort:{known:true,supported:true,levels:['low','high'],default:'high'}}],default:{model:'kiro-model',effort:'high'}};
  w.models=q=>q.get('provider')==='claude'?claude:kiro;
  const t=await boot(w).ready();t.click(t.$('settings-open'));t.click(t.$('settings-tab-roles'));await settle();
  t.click(t.$('role-open-coder'));await settle();assert.equal(t.$('role-model').tagName,'SELECT');assert.equal(t.$('role-model').value,'sonnet');
  assert.deepEqual([...t.$('role-model').options].filter(o=>!o.disabled).map(o=>o.value),['@default','sonnet','haiku']);
  const change=(id,value)=>{t.$(id).value=value;t.$(id).dispatchEvent(new t.window.Event('change',{bubbles:true}));};
  change('role-model','haiku');assert.equal(t.$('role-effort').value,'');assert.equal(t.$('role-effort').disabled,true);
  change('role-provider','kiro');await settle();assert.equal(t.$('role-model').value,'@default');change('role-model','kiro-model');assert.equal(t.$('role-effort').value,'high');
  assert.deepEqual([...t.$('role-effort').options].map(o=>o.value),['low','high']);
  let resolveOld;change('role-provider','claude');await settle();
  w.models=q=>q.get('provider')==='kiro'?new Promise(r=>resolveOld=r):claude;
  change('role-provider','kiro');change('role-provider','claude');await settle();resolveOld(kiro);await settle();
  assert.equal(t.$('role-provider').value,'claude');assert.equal(t.$('role-model').value,'@default');
  role.model='removed-model';t.click(t.$('role-open-coder'));await settle();
  assert.equal(t.$('role-model').value,'');assert.equal(t.$('role-save').disabled,true);assert.match(t.$('role-model-note').textContent,/不在.*列表/);
  w.models=()=>({status:'error',models:[],error:'请先连接 CLI'});change('role-provider','kiro');await settle();
  assert.equal(t.$('role-model').disabled,true);assert.equal(t.$('role-save').disabled,false);assert.match(t.$('role-model-note').textContent,/连接/);
  assert.equal(t.calls.some(c=>c.path==='/api/models'&&(c.post||c.query.refresh)),false);assert.equal(t.errors.length,0);
});

test('deleted selections open a new Other conversation without a notice', async () => {
  const w=newWorld({a:conv('a',[]),b:conv('b',[]),c:conv('c',[])});
  w.deleteConversation=({agent})=>{delete w.convs[agent];return respond({deleted:true})};
  const inOther=t=>{
    assert.equal(t.$('agent-title').textContent,'新对话 · 其他对话');
    assert.equal(t.$('notice').textContent,'');
    assert.equal(t.document.querySelector('.group[data-group="other"] .group-new').getAttribute('aria-pressed'),'true');
  };
  const t=await boot(w).ready();await t.openConv('a');
  t.click(t.document.querySelector('.run.selected + .delete-agent'));await settle();
  inOther(t);assert.equal(t.document.querySelector('.run[data-id="a"]'),null);
  await t.openConv('b');delete w.convs.b;await t.poll();inOther(t);
  const restored=await boot(w,{selection:{group:'g',source:'s1',agent:'b'}}).ready();
  inOther(restored);
  assert.equal(restored.calls.some(c=>c.path==='/api'&&c.query.agent),false);
});

test('one cleanup includes ended Claude and Kiro tasks and refreshes conversation history', async () => {
  const w=newWorld({});w.cleanupOverview={groups:[],totals:{conversations:2,pending:3,running:1,needs_review:0,unknown:0,cleaned:0}};
  w.cleanupAction=body=>{assert.equal(body.scope,'all');w.cleanupOverview.totals.pending=0;return respond({cleaned:3,skipped:1,errors:0,conversations:[]});};
  const t=await boot(w).ready();t.click(t.$('settings-open'));t.click(t.$('settings-tab-storage'));await settle();
  assert.match(t.$('settings-page-storage').textContent,/Claude 和 Kiro/);
  assert.match(t.$('temp-title').parentElement.textContent,/结果、日志/);
  assert.equal(t.$('temp-all').disabled,false);const reads=t.calls.filter(c=>c.path==='/api').length;
  t.click(t.$('temp-all'));await settle();
  assert.match(t.$('temp-feedback').textContent,/已清理 3 轮/);assert.match(t.$('temp-feedback').textContent,/项目代码和测试脚本保留/);
  assert.doesNotMatch(t.$('temp-feedback').textContent,/历史对话和日志保留/);
  assert.equal(t.$('temp-all').disabled,true);assert.ok(t.calls.filter(c=>c.path==='/api').length>reads);
  assert.equal(t.errors.length,0);
});

test('role identity is visible and role continuation locks model controls', async () => {
  const c=conv('coding',[]);Object.assign(c,{role_id:'coder',role_name:'编码',provider:'claude',config:{model:'sonnet',effort:'medium',source:'role'}});
  const t=await boot(newWorld({coding:c})).ready();await t.openConv('coding');
  assert.match(t.document.querySelector('.run').textContent,/编码/);
  assert.match(t.$('subtitle').textContent,/编码/);assert.equal(t.$('model-select').disabled,true);
  c.role_error='角色已停用';await t.poll();assert.match(t.$('composer-hint').textContent,/角色已停用/);
});

test('active task stops through dedicated endpoint and diagnostic is copyable', async () => {
  const c=conv('active',[],'running');c.runs[0].diagnostic={category:'permission',message:'工具权限不足',next_step:'授权后继续',detail:'Bash denied'};
  const w=newWorld({active:c});w.taskStop=payload=>{assert.equal(payload.run,'r-active');c.status='cancelled';return respond({stopped:true})};
  const t=await boot(w).ready();await t.openConv('active');
  assert.equal(t.$('task-stop').hidden,false);t.click(t.$('task-stop'));await settle();
  assert.equal(t.calls.filter(c=>c.path==='/api/tasks/stop').length,1);
  t.click(t.$('diagnostics-copy'));await settle();assert.match(t.copied.at(-1),/Bash denied/);
});

test('CLI model card saves defaults and only the connection dialog shows role overrides', async () => {
  const w=newWorld({});w.connections={default_provider:'claude',providers:[{id:'claude',name:'Claude Code',connected:false,available:false,status:'disconnected',defaults_configured:false}]};
  w.roles={revision:'r1',roles:[{id:'architect',name:'架构师',provider:null,model:null,effort:null},{id:'coder',name:'编码',provider:'kiro',model:'kiro-model',effort:'high'},{id:'tester',name:'测试',provider:'claude',model:null,effort:null},{id:'reviewer',name:'审查员',provider:'claude',model:'removed',effort:'low'}]};
  w.models=()=>({provider:'claude',status:'ok',models:[{id:'one',name:'One',selectable:true,effort:{known:true,supported:true,levels:['low','medium','high'],default:'medium'}}],default:{model:'one',effort:'high',available:true}});
  w.connectionAction=payload=>{
    if(payload.action==='connect'){
      Object.assign(w.connections.providers[0],{connected:true,available:true,status:'connected'});
      return respond({ok:true,catalog:w.models(),...w.connections});
    }
    assert.equal(payload.action,'defaults');assert.equal(payload.model,'one');assert.equal(payload.revision,'r1');
    if(payload.effort==='high'){
      assert.deepEqual(payload.overrides,{architect:{model:'one',effort:'low'},coder:{model:null,effort:null}});
      assert.deepEqual(payload.switch_roles,['architect','coder']);
    }else{
      assert.equal(payload.effort,'medium');
      assert.deepEqual(payload.overrides,{});
      assert.deepEqual(payload.switch_roles,[]);
    }
    Object.assign(w.connections.providers[0],{defaults_configured:true,defaults:{model:payload.model,effort:payload.effort}});
    return respond({ok:true,...w.connections});
  };
  const t=await boot(w).ready();t.click(t.$('settings-open'));await settle();t.click(t.$('cli-card-open-claude'));await settle();
  const card=t.$('cli-model-card');
  assert.equal(card.hidden,false);
  assert.equal(card.parentElement.nextElementSibling.contains(t.$('cli-model-list-claude')),true);
  assert.ok(t.$('cli-connect-claude').closest('.cli-config-heading').contains(t.$('cli-test-claude')));
  assert.equal(card.querySelector('#cli-model-advanced'),null);
  assert.equal(t.$('cli-model-save').closest('.cli-model-head'),t.$('cli-model-title').parentElement);
  assert.equal(t.$('cli-model-save').form,t.$('cli-model-form'));
  t.click(t.$('cli-connect-claude'));await settle();
  assert.equal(t.$('cli-model-dialog').open,true);
  assert.equal(t.$('cli-model-advanced').hidden,false);
  assert.equal(t.$('cli-model-save').closest('dialog'),t.$('cli-model-dialog'));
  assert.equal(t.$('cli-model-advanced').open,false);
  t.$('cli-model-advanced').open=true;
  assert.equal(t.$('cli-model-select-default').value,'');
  assert.equal(t.$('cli-model-roles').children.length,4);
  assert.equal(t.$('cli-model-advanced').querySelector('p'),null);
  const selectedText=id=>t.$(id).selectedOptions[0].textContent;
  assert.equal(selectedText('cli-model-select-role-architect'),'请选择模型');
  assert.equal(t.$('cli-model-select-role-coder').value,'@other');
  assert.equal(t.$('cli-model-select-role-reviewer').value,'removed');
  t.$('cli-model-select-default').value='one';
  t.$('cli-model-select-default').dispatchEvent(new t.window.Event('change'));
  assert.equal(t.$('cli-model-select-role-architect').value,'@other');
  assert.equal(selectedText('cli-model-select-role-architect'),'One');
  assert.equal(t.$('cli-effort-select-role-architect').value,t.$('cli-effort-select-default').value);
  assert.equal(t.$('cli-effort-select-role-architect').disabled,false);
  assert.equal(t.$('cli-effort-select-role-coder').disabled,true);
  assert.match(selectedText('cli-model-select-role-coder'),/保留 Kiro CLI/);
  for(const [id,value] of [['cli-model-select-role-coder','@default'],['cli-effort-select-role-architect','low'],['cli-effort-select-default','high']]){
    t.$(id).value=value;t.$(id).dispatchEvent(new t.window.Event('change'));
  }
  assert.equal(selectedText('cli-model-select-role-tester'),'One');
  assert.equal(t.$('cli-effort-select-role-tester').value,'high');
  assert.equal(t.$('cli-effort-select-role-architect').value,'low');
  assert.equal(t.$('cli-model-select-role-architect').value,'one');
  assert.equal(t.$('cli-model-select-role-reviewer').value,'removed');
  t.$('cli-model-save').click();await settle();
  assert.equal(card.hidden,false);
  assert.equal(t.$('cli-model-dialog').open,false);
  assert.equal(card.querySelector('#cli-model-advanced'),null);
  assert.equal(t.$('cli-model-roles').children.length,0);
  assert.equal(t.$('cli-model-save').closest('.cli-model-head'),t.$('cli-model-title').parentElement);
  t.$('cli-effort-select-default').value='medium';
  t.$('cli-effort-select-default').dispatchEvent(new t.window.Event('change'));
  t.$('cli-model-save').click();await settle();
  assert.equal(t.calls.filter(c=>c.body?.action==='defaults').length,2);
  assert.equal(t.$('cli-model-feedback').hidden,true);assert.equal(t.errors.length,0);
});

test('switching Claude conversations invalidates model requests and preserves explicit no-effort roles', async () => {
 const a=conv('a',[]),b=conv('b',[]);a.provider=b.provider='claude';
 const w=newWorld({a,b});w.default_provider='claude';w.providers=[{id:'claude',name:'Claude Code',available:true}];
 w.models=()=>({provider:'claude',status:'ok',models:[{id:'one',name:'One',selectable:true,effort:{known:true,supported:true,source:'cli-options',levels:['low','high'],default:'high'}}],default:{model:'one',effort:'high',available:true}});
 const t=await boot(w).ready();await t.openConv('a');const before=t.calls.filter(c=>c.path==='/api/models').length;
 await t.openConv('b');assert.ok(t.calls.filter(c=>c.path==='/api/models').length>before);
 t.ev("roleModels=[{id:'one',selectable:true,effort:{known:true,supported:true,source:'cli-options',levels:['low','high']}}];$('role-model').replaceChildren(new Option('One','one'));roleModelsLoading=false;roleEffortOptions(null)");
 assert.equal(t.$('role-effort').value,'');assert.equal(t.$('role-save').disabled,false);
});

// ---- run ----------------------------------------------------------------------------------------------------
(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try { await fn(); console.log('ok   ' + name); }
    catch (error) { failed++; console.log('FAIL ' + name + '\n' + (error && error.stack || error)); }
  }
  console.log(failed ? failed + ' failed' : 'all ' + tests.length + ' passed');
  process.exit(failed ? 1 : 0);
})();
