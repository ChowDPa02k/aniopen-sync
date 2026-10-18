import { useLayoutEffect, useState, type RefObject } from "react";

/** Fit the native table body after ProTable's filters, toolbar, header and pagination. */
export function useTableHeight(
  ref: RefObject<HTMLDivElement | null>,
  active: boolean,
) {
  const [height, setHeight] = useState(200);
  useLayoutEffect(() => {
    const container = ref.current;
    if (!active || !container) return;
    let frame = 0;
    const measure = () => {
      const content = container.firstElementChild;
      const body = container.querySelector(".ant-table-body");
      if (!content || !body) return;
      const chrome =
        content.getBoundingClientRect().height -
        body.getBoundingClientRect().height;
      setHeight(Math.max(1, Math.floor(container.clientHeight - chrome)));
    };
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    };
    const resize = new ResizeObserver(schedule);
    resize.observe(container);
    if (container.firstElementChild) resize.observe(container.firstElementChild);
    const mutations = new MutationObserver(schedule);
    mutations.observe(container, { childList: true, subtree: true });
    measure();
    return () => {
      cancelAnimationFrame(frame);
      resize.disconnect();
      mutations.disconnect();
    };
  }, [ref, active]);
  return height;
}
