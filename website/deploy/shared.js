/* Jelleo · shared chrome JS
 * Used by methodology.html, security.html (and future pages).
 * Mirrors the chrome behavior of index.html: custom cursor, scroll progress,
 * reveal-on-scroll observer, mobile nav toggle.
 *
 * Page-specific JS (live data fetch, dashboards) lives inline in each page.
 */
(function() {
  'use strict';

  // Flag the document as JS-enabled so the no-JS [data-reveal] fallback in
  // shared.css gives way. Without this class, [data-reveal] elements stay
  // visible (graceful for NoScript / corp-proxy users).
  document.documentElement.classList.add('js');

  // ============== MOBILE NAV TOGGLE ==============
  // Tolerant lookup: pages may use either id="nav-toggle" or class="nav-toggle".
  const navToggle = document.getElementById('nav-toggle')
                  || document.querySelector('.nav-toggle');
  const mobileMenu = document.getElementById('mobile-menu')
                    || document.querySelector('.mobile-menu');

  if (navToggle && mobileMenu) {
    // Inject a backdrop element behind the open menu so taps outside close it.
    let backdrop = document.querySelector('.mobile-menu-backdrop');
    if (!backdrop) {
      backdrop = document.createElement('div');
      backdrop.className = 'mobile-menu-backdrop';
      document.body.appendChild(backdrop);
    }

    function closeMenu() {
      navToggle.setAttribute('aria-expanded', 'false');
      mobileMenu.classList.remove('open');
      backdrop.classList.remove('open');
      document.body.style.overflow = '';
    }
    function openMenu() {
      navToggle.setAttribute('aria-expanded', 'true');
      mobileMenu.classList.add('open');
      backdrop.classList.add('open');
      document.body.style.overflow = 'hidden';
    }

    navToggle.addEventListener('click', () => {
      const expanded = navToggle.getAttribute('aria-expanded') === 'true';
      if (expanded) closeMenu(); else openMenu();
    });
    mobileMenu.querySelectorAll('a').forEach((a) => {
      a.addEventListener('click', closeMenu);
    });
    backdrop.addEventListener('click', closeMenu);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && mobileMenu.classList.contains('open')) closeMenu();
    });
    // Close menu if user resizes back to desktop with menu open
    window.addEventListener('resize', () => {
      if (window.innerWidth > 1024 && mobileMenu.classList.contains('open')) closeMenu();
    });
    // Close menu on bfcache restore + hashchange (browser back / in-page link)
    window.addEventListener('pageshow', closeMenu);
    window.addEventListener('hashchange', closeMenu);
  }

  // Mark active nav link with aria-current="page" so screen readers announce it.
  // Matches by pathname; ignores query/hash. Same logic as the visual `.active`
  // class but adds the a11y attribute that visual styling alone misses.
  (function markActiveNav() {
    const path = location.pathname.replace(/index\.html$/, '') || '/';
    document.querySelectorAll('.nav-center a, .mobile-menu a, .footer-col a').forEach((a) => {
      try {
        const u = new URL(a.href, location.origin);
        const ap = u.pathname.replace(/index\.html$/, '') || '/';
        if (ap === path && u.host === location.host) {
          a.setAttribute('aria-current', 'page');
        }
      } catch (e) { /* skip malformed */ }
    });
  })();

  // ============== CURSOR (snappy lerp) ==============
  const dot = document.getElementById('dot');
  const ring = document.getElementById('ring');
  const _reduceMotionCursor = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const isCoarse = window.matchMedia('(hover: none)').matches
                || window.innerWidth < 1024
                || _reduceMotionCursor;

  if (dot && ring && !isCoarse) {
    let mx = window.innerWidth / 2, my = window.innerHeight / 2;
    let rx = mx, ry = my;
    let rafId = null;
    let active = false;

    document.addEventListener('mousemove', (e) => {
      mx = e.clientX;
      my = e.clientY;
      dot.style.transform = `translate(${mx}px, ${my}px) translate(-50%, -50%)`;
      if (!active) { active = true; loop(); }
    }, { passive: true });

    function loop() {
      rx += (mx - rx) * 0.85;
      ry += (my - ry) * 0.85;
      ring.style.transform = `translate(${rx}px, ${ry}px) translate(-50%, -50%)`;
      // Idle stop — when ring is within 0.1px of target, stop the RAF loop.
      // Restart on next mousemove. Saves ~5% CPU on idle desktop sessions.
      if (Math.abs(mx - rx) < 0.1 && Math.abs(my - ry) < 0.1) {
        active = false; rafId = null; return;
      }
      rafId = requestAnimationFrame(loop);
    }

    // Single delegated mouseover/mouseout listener (was 200+ per-element
    // listeners). Catches dynamic content too.
    const HOVER_SEL = 'a, button, .pillar, .stat-cell, .toc a, .nav-cta, [role="button"]';
    document.addEventListener('mouseover', (e) => {
      if (e.target.closest && e.target.closest(HOVER_SEL)) {
        ring.classList.add('hover'); dot.classList.add('hover');
      }
    }, { passive: true });
    document.addEventListener('mouseout', (e) => {
      if (e.target.closest && e.target.closest(HOVER_SEL)) {
        // Only remove if relatedTarget isn't also a hover-target (avoids flicker).
        const next = e.relatedTarget;
        if (!next || !next.closest || !next.closest(HOVER_SEL)) {
          ring.classList.remove('hover'); dot.classList.remove('hover');
        }
      }
    }, { passive: true });
  }

  // ============== SCROLL PROGRESS + NAV CONDENSE ==============
  const progress = document.getElementById('progress');
  const nav = document.getElementById('nav');
  let isScrolled = false;
  const onScroll = () => {
    const scrolled = window.scrollY;
    const total = document.documentElement.scrollHeight - window.innerHeight;
    const pct = total > 0 ? (scrolled / total) * 100 : 0;
    if (progress) progress.style.width = pct + '%';
    if (nav) {
      // Only toggle on threshold cross — was firing classList.add 60+ times/sec
      const should = scrolled > 40;
      if (should !== isScrolled) { isScrolled = should; nav.classList.toggle('scrolled', should); }
    }
  };
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  // ============== TYPEWRITER (per-character title reveal, word-safe) ==============
  // Walks descendant text nodes and rewrites them so each WORD is wrapped in
  // a `.title-word` (display: inline-block; white-space: nowrap) and each
  // CHARACTER inside that word is wrapped in a `.title-char` with a staggered
  // animation-delay. This way the browser breaks lines BETWEEN words but
  // never WITHIN a word — even though characters are inline-block. Preserves
  // any nested HTML structure (e.g. <span class="accent">).
  function applyTypewriter(el) {
    if (!el || el.dataset.typed) return;
    el.dataset.typed = '1';
    const textNodes = [];
    (function walk(node) {
      if (node.nodeType === 3) {
        if (node.textContent.length) textNodes.push(node);
      } else if (node.nodeType === 1) {
        node.childNodes.forEach(walk);
      }
    })(el);

    let idx = 0;
    const stagger = 22; // ms per char

    function makeChar(ch) {
      const span = document.createElement('span');
      span.className = 'title-char';
      span.textContent = ch;
      span.style.animationDelay = (idx * stagger) + 'ms';
      idx++;
      return span;
    }

    for (const tn of textNodes) {
      const text = tn.textContent;
      const frag = document.createDocumentFragment();
      // Split on whitespace, preserving the whitespace as separate breakable runs
      const parts = text.split(/(\s+)/);
      for (const part of parts) {
        if (part === '') continue;
        if (/^\s+$/.test(part)) {
          // Whitespace stays as a normal text node so the browser can break here
          frag.appendChild(document.createTextNode(part));
        } else {
          // Wrap each word — characters inside are inline-block but the word
          // container has white-space: nowrap so it stays atomic.
          const wordSpan = document.createElement('span');
          wordSpan.className = 'title-word';
          for (const ch of part) wordSpan.appendChild(makeChar(ch));
          frag.appendChild(wordSpan);
        }
      }
      tn.parentNode.replaceChild(frag, tn);
    }
  }

  // ============== REVEAL-ON-SCROLL ==============
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add('in');
            // Trigger character-by-character reveal on any section-title
            // inside (or that IS) the entered element.
            const title = entry.target.querySelector('.section-title');
            if (title) applyTypewriter(title);
            if (entry.target.matches('.section-title')) applyTypewriter(entry.target);
            observer.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12, rootMargin: '0px 0px -80px 0px' }
    );
    document.querySelectorAll('[data-reveal]').forEach((el) => observer.observe(el));
  } else {
    document.querySelectorAll('[data-reveal]').forEach((el) => el.classList.add('in'));
  }

  // ============== PARTICLE NETWORK (drifting dots + connecting lines) ==============
  // Mirrors the canvas#particles animation on index.html so every page in the
  // site has the same animated background.
  // SKIP entirely on touch devices, narrow viewports, and reduced-motion users
  // — the O(N²) line-distance check + 60fps redraw drains battery on mobile
  // and is invisible behind the CSS `display: none` rule we apply there.
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const skipParticles = reduceMotion
                     || window.matchMedia('(hover: none)').matches
                     || window.innerWidth < 1024;
  const canvas = document.getElementById('particles');
  if (canvas && canvas.getContext && !skipParticles) {
    const ctx = canvas.getContext('2d');
    let dpr = window.devicePixelRatio || 1;
    let W = window.innerWidth;
    let H = window.innerHeight;

    function resize() {
      W = window.innerWidth;
      H = window.innerHeight;
      canvas.width = W * dpr;
      canvas.height = H * dpr;
      canvas.style.width = W + 'px';
      canvas.style.height = H + 'px';
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.scale(dpr, dpr);
    }
    resize();
    window.addEventListener('resize', () => {
      dpr = window.devicePixelRatio || 1;
      resize();
    });

    function particleCount() { return window.innerWidth < 768 ? 35 : 100; }
    function makeParticle() {
      return {
        x: Math.random() * W, y: Math.random() * H,
        vx: (Math.random() - 0.5) * 0.25, vy: (Math.random() - 0.5) * 0.25,
        r: Math.random() * 1.4 + 0.6, base: Math.random() * 0.5 + 0.3,
      };
    }
    let particles = Array.from({ length: particleCount() }, makeParticle);
    // Recompute COUNT on resize (was set once at load — desktop->mobile resize
    // kept 100 particles on a small viewport; mobile->desktop never expanded)
    window.addEventListener('resize', () => {
      const target = particleCount();
      while (particles.length < target) particles.push(makeParticle());
      if (particles.length > target) particles.length = target;
      // Clamp positions to new W/H (avoid stranded particles in old gutter)
      for (const p of particles) {
        if (p.x > W) p.x = W;
        if (p.y > H) p.y = H;
      }
    });

    let mouseX = -9999;
    let mouseY = -9999;
    document.addEventListener('mousemove', (e) => {
      mouseX = e.clientX;
      mouseY = e.clientY;
    }, { passive: true });

    function tick() {
      ctx.clearRect(0, 0, W, H);

      for (const p of particles) {
        p.x += p.vx;
        p.y += p.vy;
        if (p.x < 0 || p.x > W) p.vx *= -1;
        if (p.y < 0 || p.y > H) p.vy *= -1;

        const dx = p.x - mouseX;
        const dy = p.y - mouseY;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 140) {
          const force = (1 - dist / 140) * 1.2;
          p.x += (dx / dist) * force;
          p.y += (dy / dist) * force;
        }

        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(245,243,237,' + (p.base * 0.5) + ')';
        ctx.fill();
      }

      for (let i = 0; i < particles.length; i++) {
        for (let j = i + 1; j < particles.length; j++) {
          const dx = particles[i].x - particles[j].x;
          const dy = particles[i].y - particles[j].y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist < 130) {
            const alpha = (1 - dist / 130) * 0.15;
            ctx.beginPath();
            ctx.moveTo(particles[i].x, particles[i].y);
            ctx.lineTo(particles[j].x, particles[j].y);
            ctx.strokeStyle = 'rgba(245,243,237,' + alpha + ')';
            ctx.lineWidth = 1;
            ctx.stroke();
          }
        }
      }

      if (!document.hidden) tickRaf = requestAnimationFrame(tick);
    }
    let tickRaf = requestAnimationFrame(tick);
    // Pause when tab hides; resume on visible. Browsers throttle rAF on
    // hidden tabs but don't always halt; explicit cancel guarantees zero work.
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        if (tickRaf) { cancelAnimationFrame(tickRaf); tickRaf = null; }
      } else if (!tickRaf) {
        tickRaf = requestAnimationFrame(tick);
      }
    });
  }

})();
