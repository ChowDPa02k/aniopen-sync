import { useLayoutEffect, useState, type RefObject } from "react";

/** Fit desktop rows to the viewport; narrow/short layouts scroll the full page. */
export function useTableHeight(
  ref: RefObject<HTMLDivElement | null>,
  active: boolean,
) {
  const [height, setHeight] = useState<number | undefined>(200);
  useLayoutEffect(() => {
    const container = ref.current;
    if (!active || !container) return;
    let frame = 0;
    const measure = () => {
      if (
        getComputedStyle(container)
          .getPropertyValue("--task-table-page-scroll")
          .trim() === "1"
      ) {
        setHeight(undefined);
        return;
      }
      const content = container.firstElementChild;
      const body = container.querySelector(".ant-table-body, .ant-table-content");
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
