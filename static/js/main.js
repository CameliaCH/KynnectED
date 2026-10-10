// Presentation controls only; account, pairing, and session workflows stay unchanged.
document.documentElement.classList.add('js');
const menuToggle = document.querySelector('.menu-toggle');
const mainNav = document.getElementById('main-nav');
if (menuToggle && mainNav) {
  const closeMenu = () => {
    mainNav.classList.remove('is-open');
    menuToggle.setAttribute('aria-expanded', 'false');
  };
  menuToggle.addEventListener('click', () => {
    const open = mainNav.classList.toggle('is-open');
    menuToggle.setAttribute('aria-expanded', String(open));
  });
  mainNav.addEventListener('click', event => {
    if (event.target.closest('a')) closeMenu();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && mainNav.classList.contains('is-open')) {
      closeMenu();
      menuToggle.focus();
    }
  });
}

const resourceDialog = document.getElementById('resource-login');
if (resourceDialog && typeof resourceDialog.showModal === 'function') {
  document.querySelectorAll('[data-locked-folder]').forEach(link => {
    link.addEventListener('click', event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      document.getElementById('resource-login-description').textContent =
        `Log in to explore ${link.dataset.lockedFolder}. Materials will be added soon.`;
      document.getElementById('resource-login-link').onclick = () => {
        try { sessionStorage.setItem('kynnected-resource-return', new URL(link.href).pathname); } catch (_) { /* Storage may be unavailable. */ }
      };
      resourceDialog.showModal();
    });
  });
  resourceDialog.addEventListener('click', event => {
    const box = resourceDialog.getBoundingClientRect();
    if (event.target === resourceDialog && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom)) resourceDialog.close();
  });
}
// Return to the chosen library folder after the existing login redirect.
try {
  const destination = sessionStorage.getItem('kynnected-resource-return');
  if (document.body.dataset.signedIn === 'true' && destination) {
    sessionStorage.removeItem('kynnected-resource-return');
    if (/^\/learning-resources\/[a-z-]+$/.test(destination)) window.location.assign(destination);
  }
} catch (_) { /* The library remains accessible through navigation without storage. */ }

// Make existing signup choices keyboard-operable without changing their click handlers.
document.querySelectorAll('.signup-options > span').forEach(choice => {
  choice.setAttribute('role', 'button');
  choice.tabIndex = 0;
  choice.setAttribute('aria-label', choice.querySelector('img')?.alt || choice.id);
  const updatePressed = () => choice.setAttribute('aria-pressed', String(choice.style.backgroundColor === 'rgb(136, 136, 136)'));
  updatePressed();
  new MutationObserver(updatePressed).observe(choice, { attributes: true, attributeFilter: ['style'] });
  choice.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      choice.click();
    }
  });
});
