import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import {
  ProLayout,
  PageContainer,
  ProTable,
  type ActionType,
  type ProColumns,
} from "@ant-design/pro-components";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Col,
  Empty,
  Progress,
  Row,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from "antd";
import {
  DownloadOutlined,
  HistoryOutlined,
  LogoutOutlined,
  SettingOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import { api, type Obj } from "./api";
import siteIcon from "./assets/favicon.ico";
import { useTableHeight } from "./useTableHeight";
const Settings = lazy(() =>
  import("./Settings").then((module) => ({ default: module.Settings })),
);
import { statusLabels, submissionLabels, fmt, bytes } from "./utils";

export function Dashboard({ onLogout }: { onLogout: () => void }) {
  const { message, modal } = AntApp.useApp();
  const [page, setPage] = useState("tasks"),
    [status, setStatus] = useState<Obj>(),
    [error, setError] = useState("");
  const [triggering, setTriggering] = useState(false);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const navigate = (next: string) => {
    if (page === next) return;
    if (page === "settings" && settingsDirty) {
      modal.confirm({
        title: "离开配置中心？",
        content: "未保存的更改会丢失。",
        onOk: () => {
          setSettingsDirty(false);
          setPage(next);
        },
      });
    } else setPage(next);
  };
  const actionRef = useRef<ActionType>(undefined);
  const tableViewport = useRef<HTMLDivElement>(null);
  const tableHeight = useTableHeight(tableViewport, page === "tasks");
  const refresh = useCallback(async () => {
    try {
      setStatus(await api("/status"));
      setError("");
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);
  const trigger = async () => {
    setTriggering(true);
    try {
      await api("/sync", { method: "POST" });
      message.success("已开始同步");
      await refresh();
      actionRef.current?.reload();
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setTriggering(false);
    }
  };
  const columns: ProColumns<Obj>[] = [
    {
      title: "触发时间",
      dataIndex: "triggered_at",
      width: 162,
      hideInSearch: true,
      render: (_, r) => (
        <>
          <div>{fmt(r.triggered_at)}</div>
          <Typography.Text type="secondary">
            {r.trigger_type === "manual" ? "手动同步" : "定时同步"}
          </Typography.Text>
        </>
      ),
    },
    {
      title: "文件 / 剧名",
      dataIndex: "search",
      render: (_, r) => (
        <div className="release-name">
          <Typography.Text>{r.file_name}</Typography.Text>
          <div className="release-meta">
            {r.show_title}{" "}
            <Tag>
              S{String(r.season).padStart(2, "0")} · E{r.episode}
            </Tag>
          </div>
        </div>
      ),
    },
    {
      title: "提交结果",
      dataIndex: "submission",
      width: 104,
      hideInSearch: true,
      render: (_, r) => (
        <Space direction="vertical" size={4}>
          <Tag
            color={
              r.submission === "submitted"
                ? "blue"
                : r.submission === "failed"
                  ? "red"
                  : "orange"
            }
          >
            {submissionLabels[r.submission] || r.submission}
          </Tag>
          <Typography.Text type="secondary">{r.downloader}</Typography.Text>
        </Space>
      ),
    },
    {
      title: "下载状态",
      dataIndex: "status",
      width: 210,
      valueType: "select",
      valueEnum: Object.fromEntries(
        Object.entries(statusLabels)
          .filter(([k]) =>
            [
              "pending",
              "downloading",
              "waiting",
              "paused",
              "completed",
              "error",
              "removed",
              "tracking_lost",
            ].includes(k),
          )
          .map(([k, text]) => [k, { text }]),
      ),
      render: (_, r) => (
        <div>
          <div>{statusLabels[r.status] || r.status}</div>
          <Progress
            percent={Math.round(r.progress * 10) / 10}
            format={r.progress_estimated ? (percent) => `≈${percent}%` : undefined}
            size="small"
            status={
              r.status === "error"
                ? "exception"
                : r.status === "completed"
                  ? "success"
                  : "normal"
            }
          />
          <Typography.Text type="secondary" className="download-meta">
            {bytes(r.downloaded)} /{" "}
            {r.progress_estimated
              ? `约 ${bytes(r.rss_size_bytes)}`
              : bytes(r.total)}{" "}
            · {bytes(r.speed)}/s
          </Typography.Text>
        </div>
      ),
    },
    {
      title: "最近读取 / 说明",
      dataIndex: "observed_at",
      width: 180,
      hideInSearch: true,
      render: (_, r) => (
        <Space direction="vertical" size={4}>
          <Typography.Text type="secondary">
            {fmt(r.observed_at)}
          </Typography.Text>
          {r.error && (
            <Typography.Text type="danger">{r.error}</Typography.Text>
          )}
          {r.observation_error && (
            <Typography.Text type="warning">
              {r.observation_error}（保留最后状态）
            </Typography.Text>
          )}
        </Space>
      ),
    },
  ];
  return (
    <ProLayout
      className={page === "tasks" ? "sync-layout" : undefined}
      title="ANiOpen Sync"
      logo={<img className="site-icon" src={siteIcon} alt="" width={28} height={28} />}
      layout="mix"
      fixSiderbar
      fixedHeader
      siderWidth={224}
      token={{
        header: {
          colorBgHeader: "#0d1524",
          colorHeaderTitle: "#fff",
          colorTextMenu: "#c4cfe0",
          colorTextMenuSelected: "#fff",
        },
        sider: { colorMenuBackground: "#fff" },
      }}
      route={{
        routes: [
          { path: "tasks", name: "同步", icon: <DownloadOutlined /> },
          { path: "runs", name: "同步记录", icon: <HistoryOutlined /> },
          { path: "settings", name: "配置中心", icon: <SettingOutlined /> },
        ],
      }}
      location={{ pathname: page }}
      menuItemRender={(item, dom) => (
        <a
          href={"#" + (item.path || "tasks")}
          onClick={(event) => {
            event.preventDefault();
            navigate((item.path || "tasks").replace(/^\//, ""));
          }}
        >
          {dom}
        </a>
      )}
      actionsRender={() => [
        <Button
          key="logout"
          type="text"
          style={{ color: "#b9c9e0" }}
          icon={<LogoutOutlined />}
          onClick={() => {
            if (page === "settings" && settingsDirty) {
              modal.confirm({
                title: "退出控制台？",
                content: "未保存的配置会丢失。",
                onOk: onLogout,
              });
            } else onLogout();
          }}
        >
          退出
        </Button>,
      ]}
    >
      <PageContainer
        className={page === "tasks" ? "sync-page" : undefined}
        title={page === "tasks" ? "同步" : page === "runs" ? "同步记录" : "配置中心"}
        extra={
          page === "tasks"
            ? [
                <Button
                  key="sync"
                  type="primary"
                  icon={<SyncOutlined spin={status?.running} />}
                  loading={triggering}
                  disabled={status?.running}
                  onClick={trigger}
                >
                  {status?.running ? "同步进行中" : "立即同步"}
                </Button>,
              ]
            : []
        }
      >
        {error && (
          <Alert
            style={{ marginBottom: 20 }}
            type="error"
            showIcon
            message={error}
          />
        )}
        {status?.summary.uncertain > 0 && (
          <Alert
            style={{ marginBottom: 16 }}
            type="warning"
            showIcon
            message={`${status?.summary.uncertain} 个 RSS 提交结果待确认。可展开记录只读核对；确认未入队后才能重新提交。`}
          />
        )}
        {status?.observation_note && (
          <Alert
            style={{ marginBottom: 16 }}
            type="warning"
            message={status.observation_note}
          />
        )}
        {status?.mapping?.state === "cached" && (
          <Alert
            style={{ marginBottom: 16 }}
            type="warning"
            message={`在线映射暂不可用，使用 ${fmt(status.mapping.fetched_at)} 的有效缓存`}
          />
        )}
        {status?.mapping?.state === "unavailable" && (
          <Alert
            style={{ marginBottom: 16 }}
            type="warning"
            message="在线映射和同源缓存不可用，本次使用用户映射与内置规则"
          />
        )}
        {page === "tasks" && (
          <Row gutter={[16, 16]} className="stats">
            {[
              ["已创建记录", status?.summary.total],
              ["进行中 / 待确认", status?.summary.active],
              ["下载完成", status?.summary.completed],
              ["发送或下载失败", status?.summary.failed],
            ].map(([title, value]) => (
              <Col xs={12} md={6} key={title}>
                <Card>
                  <Statistic title={title} value={value ?? "—"} />
                </Card>
              </Col>
            ))}
          </Row>
        )}
        {page === "settings" ? (
          <Suspense fallback={<Card loading />}>
            <Settings onDirtyChange={setSettingsDirty} />
          </Suspense>
        ) : page === "runs" ? (
          <>
            <div className="schedule-line">
              <span
                className={"status-dot " + (status?.running ? "busy" : "")}
              />
              {status?.running
                ? "正在检查 RSS 更新"
                : !status?.source_enabled
                  ? "订阅未启用，请先配置下载器并启用订阅"
                  : !status?.schedule_enabled
                    ? "定时同步已关闭，可手动同步"
                    : "等待下一次同步"}
              <span className="next-run">
                下次触发：{fmt(status?.next_run)}
              </span>
            </div>
            <Card title="最近同步">
              <Table
                size="small"
                pagination={false}
                rowKey="id"
                scroll={{ x: 650 }}
                dataSource={status?.runs || []}
                columns={[
                  { title: "触发时间", dataIndex: "triggered_at", render: fmt },
                  {
                    title: "来源",
                    dataIndex: "trigger_type",
                    render: (v) => (v === "manual" ? "手动" : "定时"),
                  },
                  {
                    title: "结果",
                    dataIndex: "status",
                    render: (v) => (
                      <Tag
                        color={
                          v === "error"
                            ? "red"
                            : v === "success"
                              ? "green"
                              : "default"
                        }
                      >
                        {statusLabels[v] || v}
                      </Tag>
                    ),
                  },
                  { title: "已提交", dataIndex: "created" },
                  { title: "跳过", dataIndex: "skipped" },
                  { title: "说明", dataIndex: "error" },
                ]}
              />
            </Card>
          </>
        ) : (
          <div
            ref={tableViewport}
            className="tasks-table-viewport"
            style={
              tableHeight === undefined
                ? undefined
                : ({ "--task-table-body-height": `${tableHeight}px` } as CSSProperties)
            }
          >
            <ProTable<Obj>
              actionRef={actionRef}
              rowKey="id"
              columns={columns}
              polling={5000}
              search={{ labelWidth: "auto", defaultCollapsed: false }}
              pagination={{ defaultPageSize: 20, showSizeChanger: true }}
              scroll={{ x: 1100, y: tableHeight }}
              headerTitle="RSS 下载任务"
              options={{ density: true, reload: true, setting: true }}
              request={async (params) => {
                try {
                  return await api(
                    "/tasks?" +
                      new URLSearchParams({
                        page: String(params.current || 1),
                        page_size: String(params.pageSize || 20),
                        search: params.search || "",
                        status: params.status || "",
                      }),
                  );
                } catch (err) {
                  setError((err as Error).message);
                  return { data: [], total: 0, success: false };
                }
              }}
              locale={{
                emptyText: (
                  <Empty description="尚无下载任务。配置并启用 ANi 订阅后，可点击立即同步。" />
                ),
              }}
              expandable={{
                expandedRowRender: (r) => (
                  <div className="task-details">
                    {r.submission === "unknown" && (
                      <Button
                        onClick={async () => {
                          try {
                            const response = await api(
                              `/tasks/${r.id}/resolve-submission`,
                              { method: "POST", body: JSON.stringify({}) },
                            );
                            if (response.state === "found") {
                              message.success("已在引擎中确认任务");
                              actionRef.current?.reload();
                            } else if (response.can_confirm) {
                              modal.confirm({
                                title: "确认此 RSS 条目没有入队？",
                                content:
                                  "请先在下载器中核查。查询不到可能是历史已清理，不能证明从未入队。确认后仅允许下次同步重新提交此 RSS 条目，不操作已有下载任务。",
                                okText: "我已核查，确认未入队",
                                onOk: async () => {
                                  await api(
                                    `/tasks/${r.id}/resolve-submission`,
                                    {
                                      method: "POST",
                                      body: JSON.stringify({
                                        confirmed_not_submitted: true,
                                      }),
                                    },
                                  );
                                  message.success("已允许下次同步重新提交");
                                  actionRef.current?.reload();
                                },
                              });
                            } else
                              message.warning(
                                "无法确认未入队，请检查引擎连接和扫描上限",
                              );
                          } catch (err) {
                            message.error((err as Error).message);
                          }
                        }}
                      >
                        核对 RSS 提交结果
                      </Button>
                    )}
                    <Typography.Text>
                      来源地址：{r.original_url || "旧版本未记录"}
                    </Typography.Text>
                    <Typography.Text>
                      实际下载地址：{r.download_url || "—"}
                    </Typography.Text>
                    <Typography.Text copyable>
                      原始引擎标识：{r.engine_id || "尚未取得"}
                    </Typography.Text>
                    <Typography.Text copyable>
                      当前引擎标识：{r.current_engine_id || "尚未取得"}
                    </Typography.Text>
                    <Typography.Text>
                      RSS 原始文件名：{r.original_name}
                    </Typography.Text>
                    {r.auto_retry_state && (
                      <Typography.Text>
                        自动重试：{({
                          waiting: "等待重试", submitting: "正在发送", unknown: "提交结果待确认",
                          submitted: "已提交", rejected: "引擎拒绝重试", exhausted: "已达次数上限",
                          cancelled: "已取消等待",
                        } as Obj)[r.auto_retry_state] || r.auto_retry_state}
                        {` · 已尝试 ${r.auto_retry_count} 次`}
                        {r.auto_retry_due_at && ` · 计划 ${fmt(r.auto_retry_due_at)}`}
                        {r.auto_retry_error && ` · ${r.auto_retry_error}`}
                      </Typography.Text>
                    )}
                    {r.engine_attempts?.length > 0 && (
                      <Table
                        size="small"
                        rowKey="engine_id"
                        pagination={false}
                        dataSource={r.engine_attempts}
                        columns={[
                          { title: "任务 GID / Hash", dataIndex: "engine_id" },
                          {
                            title: "最后观测状态",
                            dataIndex: "status",
                            render: (v: string) =>
                              statusLabels[v] ||
                              (
                                {
                                  active: "下载中",
                                  complete: "已完成",
                                  submitted: "已提交",
                                } as Obj
                              )[v] ||
                              v,
                          },
                          {
                            title: "首次观察",
                            dataIndex: "first_seen",
                            render: fmt,
                          },
                          {
                            title: "最后观察",
                            dataIndex: "last_seen",
                            render: fmt,
                          },
                        ]}
                      />
                    )}
                  </div>
                ),
              }}
            />
          </div>
        )}
      </PageContainer>
    </ProLayout>
  );
}
