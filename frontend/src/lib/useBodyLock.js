import { useEffect } from "react";

/**
 * Freeze background scrolling while an overlay is open.
 *
 * Compensates for the scrollbar's width so locking does not shift the page
 * sideways by 15px behind the drawer - the layout jump is far more noticeable
 * than the lock itself.
 */
export function useBodyLock(active) {
  useEffect(() => {
    if (!active) return undefined;

    const { overflow, paddingRight } = document.body.style;
    const gutter = window.innerWidth - document.documentElement.clientWidth;

    document.body.style.overflow = "hidden";
    if (gutter > 0) document.body.style.paddingRight = `${gutter}px`;

    return () => {
      document.body.style.overflow = overflow;
      document.body.style.paddingRight = paddingRight;
    };
  }, [active]);
}

/** Close an overlay on Escape. */
export function useEscape(active, onClose) {
  useEffect(() => {
    if (!active) return undefined;
    const onKeyDown = (event) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [active, onClose]);
}
