/* 关系发布按钮：POST /api/publish，支持失败后再次点击从校验点续跑。 */
(() => {
  function setMessage(node, text, kind) {
    if (!node) return;
    node.textContent = text;
    node.className = 'msg' + (kind ? ' msg-' + kind : '');
  }

  document.addEventListener('DOMContentLoaded', () => {
    const button = document.getElementById('publish-btn');
    const message = document.getElementById('publish-msg');
    if (!button) return;
    button.addEventListener('click', async () => {
      button.disabled = true;
      setMessage(message, '发布中…（失败可再点，从上次校验点续跑）');
      try {
        const response = await fetch('/api/publish', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({}),
        });
        const body = await response.json().catch(() => null);
        if (response.ok) {
          const tag = body.resumed ? '续跑完成' : (body.reused ? '复用已发布' : '发布完成');
          setMessage(message, tag + ' ' + body.publish_id + '，校验点 ' +
            (body.checkpoints || []).map((c) => c.stage).join('→'), 'ok');
          window.setTimeout(() => window.location.reload(), 600);
        } else {
          const detail = body && body.detail ? '（断点 ' + body.detail.checkpoint + '）' : '';
          setMessage(message, (body && body.error || '失败') + ' ' +
            (body && body.message || '') + detail + '，再点一次可续跑', 'bad');
          button.disabled = false;
        }
      } catch (err) {
        setMessage(message, '网络失败，检查服务是否在 18206', 'bad');
        button.disabled = false;
      }
    });
  });
})();
