import { useEffect, useRef, useState } from 'react';

// Shared by privileged read-only views; a refresh revokes the previous request
// immediately, before React runs the next effect. Never replay writes here.
export default function useMetadataRead(load, onDenied, failure) {
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState({ phase: 'loading', data: null });
  const request = useRef(null);
  useEffect(() => {
    const controller = new AbortController();
    request.current = controller;
    setState({ phase: 'loading', data: null });
    Promise.resolve().then(() => {
      if (!controller.signal.aborted) return load(controller.signal);
    }).then((data) => {
      if (!controller.signal.aborted) setState({ phase: 'ready', data });
    }).catch((error) => {
      if (controller.signal.aborted) return;
      if ([401, 403].includes(error.status)) onDenied(true);
      setState({ phase: 'error', data: null, message: failure(error) });
    });
    return () => controller.abort();
  }, [load, onDenied, failure, revision]);
  return { ...state, refresh() {
    request.current?.abort();
    setState({ phase: 'loading', data: null });
    setRevision((value) => value + 1);
  } };
}
