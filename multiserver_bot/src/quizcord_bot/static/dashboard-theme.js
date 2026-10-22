(() => {
      const toggle = document.getElementById("theme-toggle");
      const label = toggle.querySelector(".theme-toggle-label");
      const themeIcon = toggle.querySelector(".theme-toggle-icon");
      const root = document.documentElement;
      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
      let transitionRunning = false;

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

      function blobClipAt(x, y, radius) {
          const points = Array.from({ length: 32 }, (_, index) => {
            const angle = (Math.PI * 2 * index) / 32;
            const factor =
              1 + 0.055 * Math.sin(3 * angle + 0.4) + 0.035 * Math.sin(7 * angle + 1.2);
            const distance = radius * factor;
            return {
              x: x + Math.cos(angle) * distance,
              y: y + Math.sin(angle) * distance
            };
          });
          return `polygon(${points.map(point => `${point.x}px ${point.y}px`).join(",")})`;
      }

      function spillFrom(button, theme) {
          if (transitionRunning) return;
          if (!document.startViewTransition) {
            applyTheme(theme);
            return;
          }

          const width = window.innerWidth;
          const height = window.innerHeight;
          const bounds = button.getBoundingClientRect();
          const x = bounds.left + bounds.width / 2;
          const y = bounds.top + bounds.height / 2;
          const maxCornerDistance = Math.max(
            Math.hypot(x, y),
            Math.hypot(width - x, y),
            Math.hypot(x, height - y),
            Math.hypot(width - x, height - y)
          );
          const endRadius = maxCornerDistance / 0.91 * 1.08;
          transitionRunning = true;
          const transition = document.startViewTransition(() => applyTheme(theme));
          if (reducedMotion.matches) {
            transition.finished
              .catch(error => {
                if (error.name !== "AbortError") {
                  console.error("The theme fade transition failed.", error);
                }
              })
              .finally(() => {
                transitionRunning = false;
              });
            return;
          }

          transition.ready
            .then(() => {
              const animation = root.animate(
                [
                  { clipPath: blobClipAt(x, y, 0) },
                  { clipPath: blobClipAt(x, y, endRadius) }
                ],
                {
                  duration: 1250,
                  easing: "cubic-bezier(0.22, 0.72, 0.25, 1)",
                  fill: "both",
                  pseudoElement: "::view-transition-new(root)"
                }
              );
              return animation.finished;
            })
            .catch(error => {
              if (error.name !== "AbortError") {
                console.error("The theme spill transition failed.", error);
              }
            })
            .finally(() => {
              transitionRunning = false;
            });
      }

      syncToggle();
      toggle.addEventListener("click", () => {
        const theme = root.dataset.theme === "dark" ? "light" : "dark";
        spillFrom(toggle, theme);
      });
    })();
