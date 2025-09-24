(function () {
  function debounce(fn, delay = 250) {
    let handle;
    return function (...args) {
      clearTimeout(handle);
      handle = setTimeout(() => fn.apply(this, args), delay);
    };
  }

  function initSidenavs() {
    const sidenavs = document.querySelectorAll('.sidenav');
    if (sidenavs.length && window.M && M.Sidenav) {
      M.Sidenav.init(sidenavs, {});
    }
  }

  function initSelects(scope = document) {
    if (!window.M || !M.FormSelect) {
      return;
    }
    const selects = scope.querySelectorAll('select');
    selects.forEach((select) => {
      const existing = window.M.FormSelect.getInstance(select);
      if (select.dataset.mInitialized && existing) {
        return;
      }
      if (existing) {
        existing.destroy();
      }
      window.M.FormSelect.init(select);
      select.dataset.mInitialized = 'true';
    });
  }

  function initTextareas(scope = document) {
    if (!window.M || !M.textareaAutoResize) {
      return;
    }
    const textareas = scope.querySelectorAll('textarea.materialize-textarea');
    textareas.forEach((textarea) => {
      M.textareaAutoResize(textarea);
    });
    if (M.updateTextFields) {
      M.updateTextFields();
    }
  }

  async function renderMarkdown(textarea, preview) {
    const content = textarea.value.trim();
    if (!content) {
      preview.innerHTML = '<span class="text-muted">Noch keine Inhalte eingegeben.</span>';
      return;
    }
    preview.innerHTML = '<span class="text-muted">Vorschau wird geladen...</span>';
    try {
      const response = await fetch('/api/markdown/preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      });
      if (!response.ok) {
        throw new Error('Serverfehler');
      }
      const payload = await response.json();
      preview.innerHTML = payload.html || '<span class="text-muted">Keine Vorschau verfügbar.</span>';
      if (window.MathJax && window.MathJax.typesetPromise) {
        window.MathJax.typesetPromise([preview]);
      }
    } catch (error) {
      preview.innerHTML = '<span class="text-muted">Vorschau konnte nicht geladen werden.</span>';
      console.error('Markdown preview failed', error);
    }
  }

  function initMarkdownEditors() {
    const editors = document.querySelectorAll('textarea[data-preview-target]');
    editors.forEach((textarea) => {
      const previewId = textarea.dataset.previewTarget;
      const preview = previewId ? document.getElementById(previewId) : null;
      if (!preview) {
        return;
      }
      const updatePreview = debounce(() => renderMarkdown(textarea, preview), 300);
      textarea.addEventListener('input', updatePreview);
      updatePreview();
    });
  }

  function initDifficultySegments() {
    document.querySelectorAll('.difficulty-segmented').forEach((segment) => {
      const targetInputId = segment.dataset.targetInput;
      const hiddenInput = targetInputId ? document.getElementById(targetInputId) : null;
      const display = segment.dataset.displayTarget
        ? document.getElementById(segment.dataset.displayTarget)
        : null;
      if (!hiddenInput) {
        return;
      }
      segment.querySelectorAll('input[type="radio"]').forEach((radio) => {
        radio.addEventListener('change', () => {
          hiddenInput.value = radio.value;
          if (display) {
            display.textContent = radio.dataset.label || `Stufe ${radio.value}`;
          }
        });
      });

      const checked = segment.querySelector('input[type="radio"]:checked');
      if (checked) {
        hiddenInput.value = checked.value;
        if (display) {
          display.textContent = checked.dataset.label || `Stufe ${checked.value}`;
        }
      }
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    initSidenavs();
    initSelects();
    initTextareas();
    initMarkdownEditors();
    initDifficultySegments();
  });

  window.AppUI = {
    initSelects,
    initTextareas,
    refresh(scope = document) {
      initSelects(scope);
      initTextareas(scope);
    },
  };
})();
