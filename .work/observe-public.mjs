// Development monitor only. Active tasks expose public status and permission labels.
export async function observePublic({ sky, fs, window }, manifestPath) {
  const actualWindow = await sky.get_window({ id: window.id, app: window.app });
  const state = await sky.get_window_state({ window: actualWindow, include_text: true, include_screenshot: true });
  const tree = state.accessibility.tree.split('Document text:')[0];
  const sidebar = tree.split(/\n[^\n]*主要\n/)[0].split('\n');
  const manifest = JSON.parse(await fs.readFile(manifestPath, 'utf8'));
  const at = new Date().toISOString();
  const rows = [];
  for (const [name, task] of Object.entries(manifest)) {
    if (!task || typeof task !== 'object' || !task.live_submitted || typeof task.ui_title !== 'string') continue;
    const index = sidebar.findIndex(line => line.trim().match(/^\d+ 按钮 (.+)$/)?.[1] === task.ui_title);
    const statusRow = index < 0 ? null : sidebar.slice(index + 1, index + 5).find(line => /Description: /.test(line));
    if (!statusRow) { rows.push({ case: name, title: task.ui_title, status: 'not_visible' }); continue; }
    task.last_public_status = statusRow.split('Description: ')[1];
    task.last_public_status_observed_at_utc = at;
    if (task.last_public_status === '运行中' && !task.first_completed_status_observed_at_utc) task.last_running_observed_at_utc = at;
    if (task.last_public_status === '已完成' && !task.first_completed_status_observed_at_utc) task.first_completed_status_observed_at_utc = at;
    rows.push({ case: name, title: task.ui_title, status: task.last_public_status });
  }
  manifest.state_capture_addendum = 'Screenshot-backed UI capture; the caller discards image blocks and receives only public task status and permission labels. This capture method does not imply a client restart or prove delivery completion or model quality.';
  await fs.writeFile(manifestPath, JSON.stringify(manifest, null, 2) + '\n', 'utf8');
  return { window: actualWindow, observed_at_utc: at, tasks: rows, permission_labels: tree.split('\n').filter(line => /按钮.*(?:允许|拒绝)|文本.*(?:授权确认|目录授权)/.test(line)) };
}
