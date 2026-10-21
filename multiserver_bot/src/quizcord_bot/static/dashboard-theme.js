(() => {
      const toggle = document.getElementById("theme-toggle");
      const label = toggle.querySelector(".theme-toggle-label");
      const themeIcon = toggle.querySelector(".theme-toggle-icon");
      const root = document.documentElement;
      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
      const blobFactors = [1, 1.08, 0.96, 1.16, 0.94, 1.10, 0.98, 1.20, 0.93, 1.08, 0.97, 1.13];

      function syncToggle() {
        const isDark = root.dataset.theme === "dark";
        label.textContent = isDark ? "Light mode" : "Dark mode";
        themeIcon.classList.toggle("icon-sun", isDark);
        themeIcon.classList.toggle("icon-moon", !isDark);
        toggle.setAttribute("aria-label", isDark ? "Switch to light mode" : "Switch to dark mode");
        toggle.setAttribute("aria-pressed", String(isDark));
      }

      function applyTheme(theme) {
        root.dataset.theme = theme;
        syncToggle();
        try {
          localStorage.setItem("quizcord-dashboard-theme", theme);
        } catch (error) {
          console.warn("Could not save the dashboard theme preference.", error);
        }

      }

      function blobPathAt(x, y, radius) {
        const points = blobFactors.map((factor, index) => {
          const angle = (Math.PI * 2 * index) / blobFactors.length;
          const distance = radius * factor;
          return {
            x: x + Math.cos(angle) * distance,
            y: y + Math.sin(angle) * distance
          };
        });
        const length = points.length;
        let path = `M ${points[0].x} ${points[0].y}`;
        for (let index = 0; index < length; index += 1) {
          const previous = points[(index + length - 1) % length];
          const start = points[index];
          const end = points[(index + 1) % length];
          const next = points[(index + 2) % length];
          const control1 = {
            x: start.x + (end.x - previous.x) / 6,
            y: start.y + (end.y - previous.y) / 6
          };
          const control2 = {
            x: end.x - (next.x - start.x) / 6,
            y: end.y - (next.y - start.y) / 6
          };
          path += ` C ${control1.x} ${control1.y}, ${control2.x} ${control2.y}, ${end.x} ${end.y}`;
        }
        return `${path} Z`;
      }

      function spillFrom(button, theme) {
        const width = window.innerWidth;
        const height = window.innerHeight;
        const bounds = button.getBoundingClientRect();
        const x = bounds.left + bounds.width / 2;
        const y = bounds.top + bounds.height / 2;
        const startRadius = Math.max(bounds.width, bounds.height) * 0.56;
        const maxCornerDistance = Math.max(
          Math.hypot(x, y),
          Math.hypot(width - x, y),
          Math.hypot(x, height - y),
          Math.hypot(width - x, height - y)
        );
        const endRadius = maxCornerDistance / Math.min(...blobFactors) * 1.08;
        const previousTheme = root.dataset.theme;
        root.dataset.theme = theme;
        const inkColor = getComputedStyle(root).getPropertyValue("--bg").trim();
        root.dataset.theme = previousTheme;
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("class", "theme-liquid");
        svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
        svg.setAttribute("aria-hidden", "true");

        const shape = document.createElementNS("http://www.w3.org/2000/svg", "path");
        shape.setAttribute("d", blobPathAt(x, y, startRadius));
        shape.style.fill = inkColor;
        svg.append(shape);
        document.body.append(svg);
        toggle.disabled = true;

        if (reducedMotion.matches || !window.requestAnimationFrame) {
          shape.setAttribute("d", `M0 0H${width}V${height}H0Z`);
          svg.style.opacity = "0";
          if (svg.animate) {
            const fadeIn = svg.animate(
              [{ opacity: 0 }, { opacity: 1 }],
              { duration: 180, easing: "ease-out", fill: "forwards" }
            );
            fadeIn.onfinish = () => {
              applyTheme(theme);
              const fadeOut = svg.animate(
                [{ opacity: 1 }, { opacity: 0 }],
                { duration: 180, easing: "ease-in", fill: "forwards" }
              );
              fadeOut.onfinish = () => {
                svg.remove();
                toggle.disabled = false;
              };
            };
          } else {
            svg.style.transition = "opacity 180ms ease";
            window.setTimeout(() => {
              svg.style.opacity = "1";
              window.setTimeout(() => {
                applyTheme(theme);
                svg.style.opacity = "0";
                window.setTimeout(() => {
                  svg.remove();
                  toggle.disabled = false;
                }, 200);
              }, 200);
            }, 0);
          }
          return;
        }

        if (!window.requestAnimationFrame) {
          applyTheme(theme);
          svg.remove();
          toggle.disabled = false;
          return;
        }

        const startedAt = performance.now();
        const duration = 1250;
        function frame(now) {
          const progress = Math.min(1, (now - startedAt) / duration);
          const eased = 1 - Math.pow(1 - progress, 3.2);
          const radius = startRadius + (endRadius - startRadius) * eased;
          shape.setAttribute("d", blobPathAt(x, y, radius));
          if (progress < 1) {
            window.requestAnimationFrame(frame);
            return;
          }
          applyTheme(theme);
          window.requestAnimationFrame(() => {
            svg.remove();
            toggle.disabled = false;
          });
        }
        window.requestAnimationFrame(frame);
      }

      syncToggle();
      toggle.addEventListener("click", () => {
        const theme = root.dataset.theme === "dark" ? "light" : "dark";
        spillFrom(toggle, theme);
      });
    })();
