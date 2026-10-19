import {
  Fragment,
  useCallback,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Modal,
  Space,
  Tabs,
  Tag,
} from "antd";
import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { stringify } from "yaml";
import { api, type Obj } from "./api";
import { FieldGroup, Fields } from "./ConfigurationFields";

type FieldSection = { title?: string; keys: string[]; before?: ReactNode };
const sourceSections: FieldSection[] = [
  {
    title: "RSS 订阅",
    keys: ["enabled", "api_type", "downloader", "rss_link", "rss_link_torrent"],
  },
  { title: "保存目录", keys: ["save_path", "classification_on_directory"] },
  {
    title: "标题与季集解析",
    keys: ["blacklist", "custom_season_mapping", "season_episode_adjustment"],
  },
  {
    title: "在线映射",
    keys: ["online_mappings_enabled", "online_mappings_url"],
  },
  {
    title: "请求与下载地址",
    keys: [
      "proxy",
      "request_timeout",
      "max_feed_bytes",
      "max_torrent_bytes",
      "url_rewrites",
    ],
  },
  {
    title: "qBittorrent 子分类",
    keys: ["use_sub_category", "custom_category_mapping"],
  },
  { keys: ["download_param"] },
];
const maintenanceSections: FieldSection[] = [
  {
    title: "历史保留",
    keys: ["history_retention_days", "history_max_records"],
  },
  { title: "提交失败重试", keys: ["retry_base_seconds", "retry_max_seconds"] },
  {
    title: "启动与备份",
    keys: ["sync_on_startup", "automatic_backups", "backup_keep"],
  },
];

export function Settings({
  onDirtyChange,
}: {
  onDirtyChange: (dirty: boolean) => void;
}) {
  const { message, modal } = AntApp.useApp();
  const [config, setConfig] = useState<Obj>(),
    [schema, setSchema] = useState<Obj>();
  const [raw, setRaw] = useState(""),
    [mode, setMode] = useState("form"),
    [saving, setSaving] = useState(false),
    [switching, setSwitching] = useState(false),
    [section, setSection] = useState("source");
  const [errors, setErrors] = useState<Record<string, boolean>>({}),
    [loadError, setLoadError] = useState(""),
    [revision, setRevision] = useState(0);
  const [name, setName] = useState(""),
    [adding, setAdding] = useState(false),
    [dirty, setDirty] = useState(false);
  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);
  const load = useCallback(async () => {
    try {
      const [data, schema] = await Promise.all([
        api("/config"),
        api("/config/schema"),
      ]);
      setConfig(data.config);
      setRaw(data.yaml);
      setSchema(schema);
      setErrors({});
      setRevision((v) => v + 1);
      setLoadError("");
      setDirty(false);
    } catch (err) {
      setLoadError((err as Error).message);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => {
      if (dirty) e.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  const fieldError = (key: string, error: boolean) =>
    setErrors((prev) => ({ ...prev, [key]: error }));
  if (loadError)
    return (
      <Alert
        type="error"
        message={loadError}
        action={<Button onClick={load}>重试</Button>}
      />
    );
  if (!config || !schema) return <Card loading />;
  const update = (cfg: Obj) => {
    setConfig(cfg);
    setDirty(true);
  };
  const rootSchema = schema;
  const fieldSchema = (key: string) =>
    rootSchema.$defs[rootSchema.properties[key].$ref.split("/").pop()!];
  const render = (key: string, only?: string[]) => (
    <Fields
      schema={fieldSchema(key)}
      root={schema}
      value={config[key]}
      onChange={(v) => update({ ...config, [key]: v })}
      path={key}
      errors={fieldError}
      names={Object.keys(config.downloaders)}
      only={only}
    />
  );
  const renderSections = (key: string, sections: FieldSection[]) => {
    const assigned = new Set(sections.flatMap((group) => group.keys));
    const remaining = Object.keys(fieldSchema(key).properties).filter(
      (name) => !assigned.has(name),
    );
    const groups = remaining.length
      ? [...sections, { title: "其他设置", keys: remaining }]
      : sections;
    return groups.map((group) => {
      const fields = (
        <>
          {group.before}
          {render(key, group.keys)}
        </>
      );
      return group.title ? (
        <FieldGroup key={group.title} title={group.title}>
          {fields}
        </FieldGroup>
      ) : (
        <Fragment key={group.keys.join(",")}>{fields}</Fragment>
      );
    });
  };
  const switchMode = async () => {
    if (switching || saving) return;
    const next = mode === "form" ? "yaml" : "form";
    if (next === "yaml" && Object.values(errors).some(Boolean)) {
      message.error("请先修复 YAML 字段");
      return;
    }
    setSwitching(true);
    try {
      if (next === "yaml") {
        setRaw(stringify(config));
      } else {
        const result = await api("/config/validate", {
          method: "POST",
          body: JSON.stringify({ yaml: raw }),
        });
        setConfig(result.config);
        setErrors({});
        setRevision((v) => v + 1);
      }
      setMode(next);
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setSwitching(false);
    }
  };
  const save = async () => {
    setSaving(true);
    try {
      await api("/config", {
        method: "PUT",
        body: JSON.stringify(mode === "yaml" ? { yaml: raw } : config),
      });
      message.success("已保存，配置和调度已生效");
      await load();
    } catch (err) {
      message.error((err as Error).message, 8);
    } finally {
      setSaving(false);
    }
  };
  return (
    <Space direction="vertical" size={20} style={{ width: "100%" }}>
      <Card>
        <div className="settings-toolbar">
          <Button
            aria-label={mode === "form" ? "YAML模式" : "表单模式"}
            aria-busy={switching}
            disabled={saving}
            loading={switching}
            onClick={switchMode}
          >
            {mode === "form" ? "YAML模式" : "表单模式"}
          </Button>
          <Space wrap className="settings-actions">
            {dirty && <Tag color="orange">未保存</Tag>}
            <Button
              disabled={saving || switching}
              onClick={() =>
                modal.confirm({
                  title: "重新载入配置？",
                  content: "未保存的更改会丢失。",
                  onOk: load,
                })
              }
            >
              重新载入
            </Button>
            <Button
              disabled={saving || switching}
              onClick={() =>
                modal.confirm({
                  title: "显示保存的下载器密钥？",
                  content:
                    "此操作会重新载入服务器配置，未保存的更改会丢失。请确认当前屏幕环境适合显示密钥。",
                  onOk: async () => {
                    const response = await api("/config/reveal", {
                      method: "POST",
                    });
                    setConfig(response.config);
                    setRaw(response.yaml);
                    setRevision((v) => v + 1);
                    setDirty(false);
                    setErrors({});
                  },
                })
              }
            >
              显示保存的密钥
            </Button>
            <Button
              type="primary"
              aria-label="保存并应用"
              loading={saving}
              disabled={
                switching ||
                (mode === "form" && Object.values(errors).some(Boolean))
              }
              onClick={save}
            >
              保存并应用
            </Button>
          </Space>
        </div>
        {mode === "yaml" ? (
          <Input.TextArea
            className="yaml"
            value={raw}
            onChange={(e) => {
              setRaw(e.target.value);
              setDirty(true);
            }}
            autoSize={{ minRows: 25, maxRows: 45 }}
            spellCheck={false}
          />
        ) : (
          <Form layout="vertical" key={revision}>
            <Tabs
              activeKey={section}
              onChange={setSection}
              items={[
                {
                  key: "source",
                  label: "ANi 订阅与解析",
                  children: renderSections("source", sourceSections),
                },
                {
                  key: "schedule",
                  label: "同步计划",
                  children: renderSections("schedule", [
                    {
                      title: "调度规则",
                      keys: [
                        "enabled",
                        "timezone",
                        "trigger",
                        "cron",
                        "interval",
                        "run_date",
                      ],
                      before: (
                        <Button
                          style={{ marginBottom: 20 }}
                          onClick={() =>
                            update({
                              ...config,
                              schedule: {
                                ...config.schedule,
                                trigger: "cron",
                                cron: { minute: "6,36", second: "30" },
                              },
                            })
                          }
                        >
                          使用每小时 06:30 / 36:30
                        </Button>
                      ),
                    },
                    {
                      title: "错过执行的处理",
                      keys: ["misfire_grace_time", "coalesce"],
                    },
                  ]),
                },
                {
                  key: "downloaders",
                  label: "下载器连接",
                  children: (
                    <>
                      {Object.entries(config.downloaders).map(
                        ([key, value]) => (
                          <FieldGroup
                            key={key}
                            title={key}
                            extra={
                              <Button
                                danger
                                type="text"
                                aria-label="移除连接"
                                icon={<DeleteOutlined />}
                                onKeyDown={(event) => event.stopPropagation()}
                                onClick={(event) => {
                                  event.stopPropagation();
                                  if (config.source.downloader === key) {
                                    message.error("请先选择其它下载器");
                                    return;
                                  }
                                  modal.confirm({
                                    title: `移除连接 ${key}？`,
                                    content:
                                      "已提交任务和历史记录会保留；该连接的任务将无法继续读取进度。",
                                    onOk: () => {
                                      const next = { ...config.downloaders };
                                      delete next[key];
                                      update({ ...config, downloaders: next });
                                      setErrors({});
                                    },
                                  });
                                }}
                              >
                                移除连接
                              </Button>
                            }
                          >
                            <Fields
                              schema={schema.$defs.Downloader}
                              root={schema}
                              value={value as Obj}
                              onChange={(v) =>
                                update({
                                  ...config,
                                  downloaders: {
                                    ...config.downloaders,
                                    [key]: v,
                                  },
                                })
                              }
                              path={"downloaders." + key}
                              errors={fieldError}
                              names={Object.keys(config.downloaders)}
                            />
                          </FieldGroup>
                        ),
                      )}
                      <Button
                        icon={<PlusOutlined />}
                        onClick={() => setAdding(true)}
                      >
                        添加下载器连接
                      </Button>
                    </>
                  ),
                },
                {
                  key: "general",
                  label: "长期运行",
                  children: (
                    <>
                      <FieldGroup title="aria2 HTTP 自动重试">
                        {render("download_retry")}
                      </FieldGroup>
                      <FieldGroup title="任务进度观察">
                        <Form.Item
                          label="进度读取间隔（秒）"
                          htmlFor="progress_poll_seconds"
                        >
                          <InputNumber
                            id="progress_poll_seconds"
                            min={5}
                            max={3600}
                            value={config.progress_poll_seconds}
                            onChange={(v) => {
                              if (v)
                                update({ ...config, progress_poll_seconds: v });
                            }}
                          />
                        </Form.Item>
                        {render("tracking")}
                      </FieldGroup>
                      {renderSections("maintenance", maintenanceSections)}
                    </>
                  ),
                },
              ]}
            />
          </Form>
        )}
      </Card>
      <Modal
        open={adding}
        title="添加下载器连接"
        onCancel={() => setAdding(false)}
        onOk={() => {
          if (!/^[a-zA-Z0-9_-]+$/.test(name) || config.downloaders[name]) {
            message.error("名称须由字母、数字、下划线或短横线组成且不能重复");
            return;
          }
          const defaults: Obj = {};
          Object.entries(schema.$defs.Downloader.properties).forEach(
            ([k, v]) => {
              defaults[k] = (v as Obj).default;
            },
          );
          update({
            ...config,
            downloaders: { ...config.downloaders, [name]: defaults },
          });
          setName("");
          setAdding(false);
        }}
      >
        <Input
          placeholder="例如 aria2-home"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </Modal>
    </Space>
  );
}
