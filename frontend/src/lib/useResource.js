import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Fetch-on-mount with abort, loading and error state.
 *
 * `loading` starts true and only becomes false once a request settles, because
 * every consumer renders a skeleton off it - a first paint of "no results"
 * before the first response has landed is the one state this must never
 * produce.
 *
 * The fetcher takes an AbortSignal and the hook cancels in flight whenever the
 * dependencies change, so a fast sequence of filter clicks cannot let an older,
 * slower response overwrite a newer one.
 *
 * @param {(signal: AbortSignal) => Promise<any>} fetcher
 * @param {any[]} deps - re-runs when these change, same contract as useEffect
 */
export function useResource(fetcher, deps = []) {
  const [state, setState] = useState({ data: null, loading: true, error: null });
  const [nonce, setNonce] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    const controller = new AbortController();
    let live = true;

    // Not a full reset to `data: null`: keeping the previous page of results
    // mounted while the next one loads is what stops the whole grid from
    // collapsing to skeletons every time a filter changes.
    setState((previous) => ({ ...previous, loading: true, error: null }));

    fetcherRef
      .current(controller.signal)
      .then((data) => {
        if (live) setState({ data, loading: false, error: null });
      })
      .catch((error) => {
        if (!live || error.name === "AbortError") return;
        setState({ data: null, loading: false, error });
      });

    return () => {
      live = false;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { ...state, reload };
}
