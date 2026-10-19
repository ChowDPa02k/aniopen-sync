import React, { useEffect, useRef, useState } from "react";
import {
  Collapse,
  Form,
  Input,
  InputNumber,
  Select,
  Switch,
  Typography,
} from "antd";
import { parse, stringify } from "yaml";
import type { Obj } from "./api";

const labels: Record<string, string> = {
  source: "ANi 订阅",
  url_rewrites: "下载域名替换规则",
  max_feed_bytes: "RSS 响应上限（字节）",
  max_torrent_bytes: "种子文件上限（字节）",
  external_rebuild_enabled: "跟踪 aria2 外部重建",
  terminal_watch_hours: "完成 / 移除后的观察窗口（小时）",
  error_poll_seconds: "错误任务读取间隔（秒）",
  completed_poll_seconds: "完成任务读取间隔（秒）",
  lost_poll_seconds: "跟踪中断后的读取间隔（秒）",
  max_tasks_per_poll: "单轮最多观察任务数",
  max_engine_results: "引擎队列扫描上限",
  history_retention_days: "完成历史保留天数",
  history_max_records: "完成历史最多保留条数",
  retry_base_seconds: "发送前失败重试起始间隔（秒）",
  retry_max_seconds: "发送前失败最大重试间隔（秒）",
  delay_seconds: "首次下载重试间隔（秒）",
  max_delay_seconds: "最大下载重试间隔（秒）",
  max_attempts: "每条 RSS 最多自动重试次数（0 为不限）",
  max_per_poll: "每轮最多重试任务数",
  error_codes: "允许重试的 aria2 错误码",
  sync_on_startup: "重启后补查 RSS",
  automatic_backups: "自动备份",
  backup_keep: "自动备份保留天数",

  schedule: "同步计划",
  downloaders: "下载器",
  enabled: "启用",
  api_type: "资源类型",
  downloader: "提交到下载器",
  rss_link: "HTTP RSS 地址",
  rss_link_torrent: "Torrent RSS 地址",
  save_path: "相对保存目录",
  classification_on_directory: "按剧名分目录",
  blacklist: "标题黑名单",
  custom_season_mapping: "自定义季映射",
  season_episode_adjustment: "分季集数修正",
  use_sub_category: "使用 qBittorrent 子分类",
  custom_category_mapping: "自定义子分类映射",
  download_param: "qBittorrent 提交参数",
  category: "分类",
  tags: "标签",
  use_auto_torrent_management: "自动 Torrent 管理",
  online_mappings_enabled: "启用在线映射",
  online_mappings_url: "在线映射地址",
  proxy: "RSS / 映射请求代理",
  request_timeout: "RSS 请求超时（秒）",
  timezone: "时区",
  trigger: "触发方式",
  cron: "Cron 字段",
  interval: "固定间隔参数",
  run_date: "指定运行时间（ISO 8601）",
  misfire_grace_time: "错过执行的容忍时间（秒）",
  coalesce: "合并错过的多次执行",
  type: "下载引擎",
  endpoint: "API 地址",
  download_base_path: "下载器中的保存根目录",
  secret: "RPC Secret",
  username: "用户名",
  password: "密码",
  verify_certificate: "校验 HTTPS 证书",
  timeout: "API 请求超时（秒）",
  progress_poll_seconds: "进度读取间隔（秒）",
};
const hints: Record<string, string> = {
  url_rewrites: "主机名 → 主机名；填写 {} 可禁用。",
  terminal_watch_hours:
    "窗口内低频检查完成任务是否被回调重建；故障任务继续低频观察。",
  history_retention_days: "仅清理稳定完成 / 移除的展示历史，永久保留去重标识。",
  automatic_backups:
    "每 6 小时更新当天的备份，最多保留配置的天数。备份包含下载器凭据，请妥善保存。",
  cron: "支持 year / month / day / week / day_of_week / hour / minute / second / start_date / end_date / jitter。星期用 mon–sun。",
  interval:
    "支持 weeks / days / hours / minutes / seconds / start_date / end_date / jitter。",
  custom_season_mapping:
    "YAML 映射。例如：刀匠村篇: 4；也支持 season 与 reserve_keywords 对象。季号 0 表示特别篇（S00）。用户规则优先于在线映射。",
  season_episode_adjustment: "YAML 映射：剧名 → 季号 → 集数偏移量。",
  custom_category_mapping: "YAML 映射：标题关键字 → 分类路径。",
  save_path: "此目录相对于下载器的保存根目录。默认 TV/ANi。",
  download_base_path: "这是下载器容器内的路径，ANiOpen Sync 无需挂载媒体目录。",
  use_auto_torrent_management: "启用后保存位置由 qBittorrent 分类设置决定。",
  delay_seconds: "失败后等待此间隔再重建；连续失败时指数退避至最大间隔。",
  error_codes:
    "YAML 数字列表。默认重试网络故障与 HTTP 响应错误（含 403），不重试磁盘和权限错误。",
  online_mappings_enabled:
    "网络不可用时使用同一地址的缓存；没有缓存时使用内置规则和用户映射。",
};
function YamlField({
  value,
  onChange,
  onError,
  path,
}: {
  value: any;
  onChange: (v: any) => void;
  onError: (key: string, error: boolean) => void;
  path: string;
}) {
  const [text, setText] = useState(() => stringify(value));
  const [error, setError] = useState("");
  const lastValue = useRef(value);
  useEffect(() => {
    if (value !== lastValue.current) {
      lastValue.current = value;
      setText(stringify(value));
      setError("");
      onError(path, false);
    }
  }, [value, onError, path]);
  return (
    <>
      <Input.TextArea
        id={path}
        value={text}
        autoSize={{ minRows: 3, maxRows: 14 }}
        spellCheck={false}
        className="yaml"
        status={error ? "error" : undefined}
        onChange={(e) => {
          setText(e.target.value);
          try {
            const parsed = parse(e.target.value);
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
              throw new Error("请填写 YAML 对象；空映射请填 {}");
            lastValue.current = parsed;
            onChange(parsed);
            setError("");
            onError(path, false);
          } catch (err) {
            setError((err as Error).message);
            onError(path, true);
          }
        }}
      />
      {error && <Typography.Text type="danger">{error}</Typography.Text>}
    </>
  );
}

export function FieldGroup({
  title,
  children,
  extra,
}: {
  title: React.ReactNode;
  children: React.ReactNode;
  extra?: React.ReactNode;
}) {
  return (
    <Collapse
      className="settings-group"
      defaultActiveKey={["fields"]}
      items={[
        { key: "fields", label: title, children, extra, forceRender: true },
      ]}
    />
  );
}

export function Fields({
  schema,
  root,
  value,
  onChange,
  path = "",
  errors,
  names,
  only,
}: {
  schema: Obj;
  root: Obj;
  value: Obj;
  onChange: (v: Obj) => void;
  path?: string;
  errors: (k: string, e: boolean) => void;
  names: string[];
  only?: string[];
}) {
  const resolved = (s: Obj): Obj =>
    s.$ref ? root.$defs[s.$ref.split("/").pop()!] : s;
  return (
    <>
      {Object.entries(schema.properties || {}).map(([key, raw]) => {
        if (only && !only.includes(key)) return null;
        const field = resolved(raw as Obj),
          current = value[key],
          fullPath = path + "." + key;
        const change = (next: any) => onChange({ ...value, [key]: next });
        if (
          (key === "cron" && value.trigger !== "cron") ||
          (key === "interval" && value.trigger !== "interval") ||
          (key === "run_date" && value.trigger !== "date")
        )
          return null;
        if (
          path.startsWith("downloaders.") &&
          ((value.type === "aria2" && ["username", "password"].includes(key)) ||
            (value.type === "qbittorrent" && key === "secret"))
        )
          return null;
        if (field.properties)
          return (
            <FieldGroup key={key} title={labels[key] || key}>
              <Fields
                schema={field}
                root={root}
                value={current}
                onChange={change}
                path={fullPath}
                errors={errors}
                names={names}
              />
            </FieldGroup>
          );
        let input: React.ReactNode;
        if (field.enum)
          input = (
            <Select
              id={fullPath}
              value={current}
              options={field.enum.map((v: string) => ({
                value: v,
                label:
                  (
                    {
                      cron: "Cron · 日历时间",
                      interval: "Interval · 固定间隔",
                      date: "Date · 指定时间",
                      http: "HTTP 直链",
                      torrent: "Torrent",
                    } as Obj
                  )[v] || v,
              }))}
              onChange={change}
            />
          );
        else if (key === "downloader")
          input = (
            <Select
              id={fullPath}
              value={current}
              options={names.map((name) => ({ value: name, label: name }))}
              onChange={change}
            />
          );
        else if (field.type === "boolean")
          input = <Switch id={fullPath} checked={current} onChange={change} />;
        else if (field.type === "integer" || field.type === "number")
          input = (
            <InputNumber
              id={fullPath}
              value={current}
              min={field.minimum}
              max={field.maximum}
              onChange={(v) => {
                if (v !== null) change(v);
              }}
            />
          );
        else if (field.type === "array" && field.items?.type === "integer")
          input = (
            <YamlField
              value={current}
              onChange={change}
              onError={errors}
              path={fullPath}
            />
          );
        else if (field.type === "array")
          input = (
            <Select
              id={fullPath}
              mode="tags"
              value={current}
              onChange={change}
              tokenSeparators={["\n"]}
              placeholder="输入后按回车添加"
            />
          );
        else if (field.type === "object" || field.additionalProperties)
          input = (
            <YamlField
              value={current}
              onChange={change}
              onError={errors}
              path={fullPath}
            />
          );
        else if (key === "password" || key === "secret")
          input = (
            <Input.Password
              id={fullPath}
              value={current}
              onChange={(e) => change(e.target.value)}
              autoComplete="new-password"
            />
          );
        else
          input = (
            <Input
              id={fullPath}
              value={current}
              onChange={(e) => change(e.target.value)}
            />
          );
        return (
          <Form.Item
            htmlFor={fullPath}
            key={key}
            label={labels[key] || key}
            extra={
              path === "download_retry" && key === "enabled"
                ? "仅重试本项目创建的 aria2 HTTP 失败任务。启用前请停用下载器端的失败重建回调，避免重复创建任务。"
                : hints[key]
            }
          >
            {input}
          </Form.Item>
        );
      })}
    </>
  );
}
