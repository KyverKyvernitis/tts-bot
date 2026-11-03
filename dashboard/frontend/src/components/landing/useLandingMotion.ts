import { useEffect, type RefObject } from "react";

/** Progressively enhances the landing; the CSS composition is complete without GSAP. */
export function useLandingMotion(rootRef: RefObject<HTMLDivElement>): void {
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let disposed = false;
    let generation = 0;
    let teardown: (() => void) | undefined;

    const stop = () => {
      generation += 1;
      teardown?.();
      teardown = undefined;
    };

    const start = async () => {
      const currentGeneration = ++generation;
      try {
        const [{ gsap }, { ScrollTrigger }] = await Promise.all([
          import("gsap"),
          import("gsap/ScrollTrigger"),
        ]);
        // React StrictMode, navigation and preference changes can invalidate an import.
        if (disposed || currentGeneration !== generation || reducedMotion.matches || !root.isConnected) return;

        gsap.registerPlugin(ScrollTrigger);
        const media = gsap.matchMedia();
        const imageListeners: Array<() => void> = [];
        let refreshFrame = 0;

        const refresh = () => {
          if (disposed || currentGeneration !== generation) return;
          window.cancelAnimationFrame(refreshFrame);
          refreshFrame = window.requestAnimationFrame(() => {
            if (!disposed && currentGeneration === generation) ScrollTrigger.refresh();
          });
        };

        const context = gsap.context(() => {}, root);
        teardown = () => {
          window.cancelAnimationFrame(refreshFrame);
          imageListeners.forEach((remove) => remove());
          media.revert();
          context.revert();
        };

        context.add(() => {
          media.add(
            { desktop: "(min-width: 900px)", mobile: "(max-width: 899px)" },
            (match) => {
              const desktop = Boolean(match.conditions?.desktop);
              const heroCopy = root.querySelectorAll<HTMLElement>("[data-hero-copy]");
              const heroArt = root.querySelectorAll<HTMLElement>("[data-hero-art]");

              gsap.from(heroCopy, {
                y: desktop ? 24 : 14,
                opacity: 0.55,
                duration: 0.8,
                stagger: 0.09,
                ease: "power3.out",
                clearProps: "transform,opacity",
              });
              gsap.from(heroArt, {
                y: desktop ? 32 : 16,
                rotation: desktop ? 3 : 0,
                opacity: 0.45,
                duration: 1.05,
                ease: "power3.out",
                clearProps: "transform,opacity",
              });

              root.querySelectorAll<HTMLElement>("[data-reveal]").forEach((element) => {
                gsap.from(element, {
                  y: desktop ? 26 : 14,
                  opacity: 0.5,
                  duration: 0.7,
                  ease: "power2.out",
                  clearProps: "transform,opacity",
                  scrollTrigger: { trigger: element, start: "top 92%", once: true },
                });
              });

              root.querySelectorAll<HTMLElement>("[data-parallax]").forEach((element) => {
                gsap.fromTo(element, { y: 0 }, {
                  y: desktop ? -32 : -10,
                  ease: "none",
                  scrollTrigger: {
                    trigger: element.parentElement || element,
                    start: "top bottom",
                    end: "bottom top",
                    scrub: 0.6,
                  },
                });
              });

              const steps = Array.from(root.querySelectorAll<HTMLElement>("[data-story-step]"));
              const stage = root.querySelector<HTMLElement>("[data-story-stage]");
              const progress = root.querySelector<HTMLElement>("[data-story-progress]");

              if (desktop && stage && steps.length) {
                const scenes = ["call", "message", "voice"];
                scenes.forEach((name, index) => {
                  const scene = stage.querySelector<HTMLElement>(`[data-scene="${name}"]`);
                  const step = steps[index];
                  if (!scene || !step) return;
                  gsap.timeline({
                    scrollTrigger: { trigger: step, start: "top 80%", end: "bottom 20%", scrub: 0.4 },
                  })
                    .fromTo(scene, { opacity: 0.38, y: 16, scale: 0.97 }, {
                      opacity: 1, y: 0, scale: 1, duration: 0.35, ease: "power1.out",
                    })
                    .to(scene, { opacity: 0.42, y: -8, scale: 0.985, duration: 0.3 }, 0.7);
                });

                if (progress) gsap.fromTo(progress, { scaleY: 0 }, {
                  scaleY: 1,
                  transformOrigin: "top center",
                  ease: "none",
                  scrollTrigger: {
                    trigger: steps[0], endTrigger: steps[steps.length - 1],
                    start: "top 60%", end: "bottom 60%", scrub: 0.3,
                  },
                });
              }

              const voiceBars = stage?.querySelectorAll<HTMLElement>("[data-voice-bar]");
              if (voiceBars?.length && steps[2]) gsap.fromTo(voiceBars, { scaleY: 0.3 }, {
                scaleY: 1,
                transformOrigin: "center",
                stagger: { each: 0.035, from: "center" },
                ease: "none",
                scrollTrigger: {
                  trigger: desktop ? steps[2] : stage,
                  start: "top 85%", end: "bottom 30%", scrub: 0.4,
                },
              });
            },
            root,
          );
        });

        root.querySelectorAll("img").forEach((image) => {
          if (image.complete) return;
          image.addEventListener("load", refresh, { once: true });
          image.addEventListener("error", refresh, { once: true });
          imageListeners.push(() => {
            image.removeEventListener("load", refresh);
            image.removeEventListener("error", refresh);
          });
        });
        void document.fonts?.ready.then(refresh);
        refresh();
      } catch {
        // A failed optional motion chunk must leave the static page fully usable.
        if (!disposed && currentGeneration === generation) stop();
      }
    };

    const preferenceChanged = () => {
      stop();
      if (!reducedMotion.matches) void start();
    };
    reducedMotion.addEventListener("change", preferenceChanged);
    if (!reducedMotion.matches) void start();

    return () => {
      disposed = true;
      reducedMotion.removeEventListener("change", preferenceChanged);
      stop();
    };
  }, [rootRef]);
}
