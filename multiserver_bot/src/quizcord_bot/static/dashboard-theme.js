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
                  { clipPath: `circle(0px at ${x}px ${y}px)` },
                  { clipPath: `circle(${endRadius}px at ${x}px ${y}px)` }
                ],
                {
                  duration: 800,
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
