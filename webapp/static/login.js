const params = new URLSearchParams(location.search);
const reason = params.get('reason');
if (reason === 'idle') {
  const n = document.getElementById('notice');
  n.textContent = 'Signed out after 10 minutes of inactivity.';
  n.style.display = 'block';
} else if (reason === 'session') {
  const n = document.getElementById('notice');
  n.textContent = 'Your session has expired. Please sign in again.';
  n.style.display = 'block';
}

async function doLogin(e) {
  e.preventDefault();
  const btn = document.getElementById('submit-btn');
  const err = document.getElementById('error-msg');
  err.style.display = 'none';
  btn.disabled = true;
  btn.textContent = 'Signing in…';

  try {
    const res = await fetch('/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: document.getElementById('username').value,
        password: document.getElementById('password').value,
      }),
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      document.getElementById('password').value = '';
      window.location.href = '/';
    } else {
      err.textContent = data.detail || 'Invalid credentials';
      err.style.display = 'block';
      document.getElementById('password').value = '';
      document.getElementById('password').focus();
    }
  } catch (ex) {
    err.textContent = 'Connection error — is the server running?';
    err.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sign In';
  }
}
