export const statusLabels: Record<string, string> = {
  pending: "等待确认",
  downloading: "下载中",
  waiting: "排队中",
  paused: "已暂停",
  completed: "已完成",
  error: "下载失败",
  removed: "已移除",
  tracking_lost: "跟踪中断",
  success: "成功",
  partial: "部分失败",
  disabled: "订阅未启用",
  running: "运行中",
  interrupted: "进程中断",
};
export const submissionLabels: Record<string, string> = {
  preparing: "准备中",
  submitting: "发送中",
  submitted: "已提交",
  unknown: "结果待确认",
  failed: "提交失败",
};
const missingTaskMessages = new Set([
  "Task Missing",
  "引擎中未找到任务，可能已清理历史记录",
  "GID 已不可见，暂未发现可唯一关联的外部重建任务",
]);
export const observationErrorText = (value: string) =>
  missingTaskMessages.has(value) ? "任务已被清理" : value;
export const fmt = (value?: string) =>
  value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
export const bytes = (value: number = 0) =>
  value >= 1073741824
    ? `${(value / 1073741824).toFixed(2)} GB`
    : value >= 1048576
      ? `${(value / 1048576).toFixed(1)} MB`
      : `${(value / 1024).toFixed(1)} KB`;
