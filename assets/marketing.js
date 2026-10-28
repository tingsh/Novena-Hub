import './styles/marketing.css';

function bindInquiryForm() {
  document.body.dataset.marketingJsReady = 'true';
  const form = document.querySelector('[data-marketing-inquiry]');
  if (!form) return;

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const result = form.querySelector('#sales-inquiry-result');
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    result.textContent = '';
    try {
      const response = await fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        headers: {'X-CSRFToken': form.querySelector('[name="csrfmiddlewaretoken"]').value},
        credentials: 'same-origin',
      });
      result.innerHTML = await response.text();
      if (response.ok) form.reset();
    } catch {
      result.textContent = 'We could not send the request. Please try again later.';
    } finally {
      button.disabled = false;
    }
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bindInquiryForm);
} else {
  bindInquiryForm();
}
