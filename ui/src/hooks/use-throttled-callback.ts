"use client";

import { useCallback, useEffect, useRef } from "react";

export function useThrottledCallback(
  callback: () => void,
  delay: number,
): () => void {
  const callbackRef = useRef(callback);
  const lastInvocationRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    };
  }, []);

  return useCallback(() => {
    const invoke = () => {
      lastInvocationRef.current = Date.now();
      callbackRef.current();
    };
    const remaining = delay - (Date.now() - lastInvocationRef.current);

    if (remaining <= 0) {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      invoke();
      return;
    }

    if (timerRef.current === null) {
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        invoke();
      }, remaining);
    }
  }, [delay]);
}
