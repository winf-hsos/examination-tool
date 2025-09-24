(function () {
  const AppShell = {
    name: 'AppShell',
    props: {
      navLinks: {
        type: Array,
        default: () => [],
      },
      currentPath: {
        type: String,
        default: '/',
      },
      brand: {
        type: Object,
        default: () => ({}),
      },
    },
    data() {
      return {
        menuOpen: false,
        currentYear: new Date().getFullYear(),
      };
    },
    computed: {
      normalizedBrand() {
        return {
          title: 'Examination Tool',
          href: '/',
          icon: 'draw',
          subtitle: '',
          ...this.brand,
        };
      },
    },
    methods: {
      isActive(href) {
        if (!href) {
          return false;
        }
        if (href === '/') {
          return this.currentPath === '/';
        }
        return this.currentPath.startsWith(href);
      },
      closeMenu() {
        this.menuOpen = false;
      },
      handleResize() {
        if (window.innerWidth >= 960 && this.menuOpen) {
          this.menuOpen = false;
        }
      },
    },
    mounted() {
      window.addEventListener('resize', this.handleResize);
    },
    beforeUnmount() {
      window.removeEventListener('resize', this.handleResize);
    },
    template: `
      <div class="app-shell">
        <header class="app-header">
          <div class="app-header__inner page-frame">
            <a :href="normalizedBrand.href" class="brand" rel="home">
              <span class="brand__icon material-icons" aria-hidden="true">{{ normalizedBrand.icon }}</span>
              <span class="brand__text">
                <span class="brand__title">{{ normalizedBrand.title }}</span>
                <span v-if="normalizedBrand.subtitle" class="brand__subtitle">{{ normalizedBrand.subtitle }}</span>
              </span>
            </a>
            <nav class="primary-nav" aria-label="Hauptnavigation">
              <button
                class="nav-toggle"
                type="button"
                :aria-expanded="menuOpen.toString()"
                aria-controls="primary-nav"
                @click="menuOpen = !menuOpen"
              >
                <span class="sr-only">Navigation {{ menuOpen ? 'schließen' : 'öffnen' }}</span>
                <span class="material-icons" aria-hidden="true">{{ menuOpen ? 'close' : 'menu' }}</span>
              </button>
              <ul id="primary-nav" class="primary-nav__list" :class="{ 'is-open': menuOpen }">
                <li v-for="link in navLinks" :key="link.href" class="primary-nav__item">
                  <a
                    :href="link.href"
                    class="primary-nav__link"
                    :class="{ 'is-active': isActive(link.href) }"
                    @click="closeMenu"
                  >
                    {{ link.label }}
                  </a>
                </li>
              </ul>
            </nav>
          </div>
        </header>
        <main class="app-main">
          <div class="page-frame container">
            <slot />
          </div>
        </main>
        <footer class="app-footer">
          <div class="page-frame">
            <p class="app-footer__text">&copy; {{ currentYear }} Examination Tool</p>
          </div>
        </footer>
      </div>
    `,
  };

  const PageHeader = {
    name: 'PageHeader',
    props: {
      title: {
        type: String,
        required: true,
      },
    },
    template: `
      <header class="page-header">
        <div class="page-header__body">
          <p v-if="$slots.lead" class="page-header__eyebrow">
            <slot name="lead" />
          </p>
          <h1 class="page-title">{{ title }}</h1>
          <p v-if="$slots.subtitle" class="page-header__subtitle">
            <slot name="subtitle" />
          </p>
        </div>
        <div v-if="$slots.actions" class="page-header__actions">
          <slot name="actions" />
        </div>
      </header>
    `,
  };

  const SectionBlock = {
    name: 'SectionBlock',
    inheritAttrs: false,
    props: {
      title: {
        type: String,
        required: true,
      },
      subtitle: {
        type: String,
        default: '',
      },
    },
    template: `
      <section class="section-block" v-bind="$attrs">
        <header class="section-block__header">
          <div class="section-block__meta">
            <h2 class="section-block__title">{{ title }}</h2>
            <p v-if="subtitle" class="section-block__subtitle">{{ subtitle }}</p>
          </div>
          <div v-if="$slots.actions" class="section-block__actions">
            <slot name="actions" />
          </div>
        </header>
        <div class="section-block__content">
          <slot />
        </div>
      </section>
    `,
  };

  const AppCard = {
    name: 'AppCard',
    inheritAttrs: false,
    props: {
      variant: {
        type: String,
        default: 'surface',
      },
      padding: {
        type: String,
        default: 'lg',
      },
    },
    computed: {
      classes() {
        return ['app-card', `app-card--${this.variant}`, `app-card--${this.padding}`];
      },
    },
    template: `
      <article :class="classes" v-bind="$attrs">
        <slot />
      </article>
    `,
  };

  const StatCard = {
    name: 'StatCard',
    props: {
      title: {
        type: String,
        required: true,
      },
      value: {
        type: [String, Number],
        required: true,
      },
      description: {
        type: String,
        default: '',
      },
      linkLabel: {
        type: String,
        default: '',
      },
      linkHref: {
        type: String,
        default: '',
      },
      accent: {
        type: String,
        default: 'indigo',
      },
    },
    computed: {
      classes() {
        return ['stat-card', `stat-card--${this.accent}`];
      },
    },
    template: `
      <article :class="classes">
        <div v-if="$slots.icon" class="stat-card__icon">
          <slot name="icon" />
        </div>
        <div class="stat-card__body">
          <h2 class="stat-card__title">{{ title }}</h2>
          <p class="stat-card__value">{{ value }}</p>
          <p v-if="description" class="stat-card__description">{{ description }}</p>
        </div>
        <a v-if="linkHref" :href="linkHref" class="button button--ghost stat-card__cta">
          {{ linkLabel || 'Details ansehen' }}
        </a>
      </article>
    `,
  };

  function debounce(fn, delay = 250) {
    let handle;
    return function (...args) {
      window.clearTimeout(handle);
      handle = window.setTimeout(() => fn.apply(this, args), delay);
    };
  }

  function autoResizeTextarea(textarea) {
    const el = textarea;
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  }

  function initTextareas(scope = document) {
    scope.querySelectorAll('textarea').forEach((textarea) => {
      if (textarea.dataset.autosizeBound) {
        return;
      }
      textarea.dataset.autosizeBound = 'true';
      const handler = () => autoResizeTextarea(textarea);
      textarea.addEventListener('input', handler);
      window.addEventListener('resize', handler);
      autoResizeTextarea(textarea);
    });
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
      if (window.MathJax?.typesetPromise) {
        window.MathJax.typesetPromise([preview]);
      }
    } catch (error) {
      preview.innerHTML = '<span class="text-muted">Vorschau konnte nicht geladen werden.</span>';
      console.error('Markdown preview failed', error);
    }
  }

  function initMarkdownEditors(scope = document) {
    scope.querySelectorAll('textarea[data-preview-target]').forEach((textarea) => {
      if (textarea.dataset.markdownBound) {
        return;
      }
      const previewId = textarea.dataset.previewTarget;
      const preview = previewId ? document.getElementById(previewId) : null;
      if (!preview) {
        return;
      }
      const updatePreview = debounce(() => renderMarkdown(textarea, preview), 300);
      textarea.addEventListener('input', updatePreview);
      textarea.dataset.markdownBound = 'true';
      updatePreview();
    });
  }

  function initDifficultySegments(scope = document) {
    scope.querySelectorAll('.difficulty-segmented').forEach((segment) => {
      if (segment.dataset.segmentBound) {
        return;
      }
      segment.dataset.segmentBound = 'true';
      const targetInputId = segment.dataset.targetInput;
      const hiddenInput = targetInputId ? document.getElementById(targetInputId) : null;
      const display = segment.dataset.displayTarget
        ? document.getElementById(segment.dataset.displayTarget)
        : null;

      segment.querySelectorAll('input[type="radio"]').forEach((radio) => {
        radio.addEventListener('change', () => {
          if (hiddenInput) {
            hiddenInput.value = radio.value;
          }
          if (display) {
            display.textContent = radio.dataset.label || `Stufe ${radio.value}`;
          }
        });
      });

      const checked = segment.querySelector('input[type="radio"]:checked');
      if (checked) {
        if (hiddenInput) {
          hiddenInput.value = checked.value;
        }
        if (display) {
          display.textContent = checked.dataset.label || `Stufe ${checked.value}`;
        }
      }
    });
  }

  function applyEnhancements(scope = document) {
    initTextareas(scope);
    initMarkdownEditors(scope);
    initDifficultySegments(scope);
  }

  document.addEventListener('DOMContentLoaded', () => {
    applyEnhancements();
  });

  const initialState = window.__APP_INITIAL_STATE__ || {};
  const app = Vue.createApp({
    setup() {
      const navLinks = Vue.ref(initialState.navLinks || []);
      const currentPath = Vue.ref(initialState.currentPath || window.location.pathname);
      const brand = Vue.ref(initialState.brand || {});

      Vue.onMounted(() => {
        applyEnhancements();
      });

      return {
        navLinks,
        currentPath,
        brand,
      };
    },
  });

  app.component('AppShell', AppShell);
  app.component('PageHeader', PageHeader);
  app.component('SectionBlock', SectionBlock);
  app.component('AppCard', AppCard);
  app.component('StatCard', StatCard);

  app.mount('#app');

  window.AppUI = {
    initSelects(scope = document) {
      applyEnhancements(scope);
    },
    initTextareas(scope = document) {
      initTextareas(scope);
    },
    refresh(scope = document) {
      applyEnhancements(scope);
    },
  };
})();
