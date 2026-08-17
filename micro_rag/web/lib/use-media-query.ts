"use client";

import { useCallback, useSyncExternalStore } from "react";

/**
 * T47.6 — reads a media query, for the cases where the three responsive forms of a
 * component are structurally different rather than just differently styled (the evidence
 * surface is an in-flow panel at ≥1440 and a fixed overlay below it; no amount of CSS
 * makes one element both without rendering it twice).
 *
 * `useSyncExternalStore` rather than useState+useEffect: matchMedia IS an external store,
 * and subscribing to it this way means there is no post-mount setState and so no cascading
 * render on every mount. The server snapshot is `false`, which every caller must treat as
 * "the smallest layout" — that degrades to the bottom sheet, which is the safe default.
 */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      const mql = window.matchMedia(query);
      mql.addEventListener("change", onStoreChange);
      return () => mql.removeEventListener("change", onStoreChange);
    },
    [query]
  );

  const getSnapshot = useCallback(() => window.matchMedia(query).matches, [query]);
  const getServerSnapshot = useCallback(() => false, []);

  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
