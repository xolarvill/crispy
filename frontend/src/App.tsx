import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  Activity,
  ArrowDown,
  ArrowUpRight,
  BookOpen,
  CalendarDays,
  Check,
  ChevronRight,
  CircleHelp,
  Copy,
  ExternalLink,
  FolderClosed,
  Image,
  LayoutDashboard,
  ListFilter,
  LoaderCircle,
  Menu,
  PanelRightClose,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  X,
  type LucideIcon,
} from "lucide-react";
import * as Tooltip from "@radix-ui/react-tooltip";
import * as Dialog from "@radix-ui/react-dialog";
import { Button } from "./components/ui/button";
import {
  api,
  demoRuns,
  modes,
  plan,
  progress,
  stageNames,
  statusNames,
  type Run,
} from "./data";

function IconButton({
  icon: Icon,
  label,
  onClick,
  disabled = false,
}: {
  icon: LucideIcon;
  label: string;
  onClick?: () => void;
  disabled?: boolean;
}) {
  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>
        <Button
          size="icon"
          variant="ghost"
          aria-label={label}
          onClick={onClick}
          disabled={disabled}
        >
          <Icon size={17} />
        </Button>
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content className="tooltip" sideOffset={6}>
          {label}
          <Tooltip.Arrow />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}
function Status({ value }: { value: string }) {
  return (
    <span className={`status status-${value}`}>
      <span className="status-dot" />
      {statusNames[value] || value}
    </span>
  );
}
const tabs = [
  ["all", "全部"],
  ["running", "进行中"],
  ["waiting_review", "待审核"],
  ["completed", "已完成"],
] as const;
const primaryNav: [string, string, LucideIcon][] = [
  ["home", "工作台", LayoutDashboard],
  ["runs", "创意生产", FolderClosed],
  ["assets", "产品素材", Image],
];
function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
    </label>
  );
}

export default function App() {
  const modeRef = useRef(true);
  const [demo, setDemo] = useState(
    () => new URLSearchParams(location.search).get("mode") !== "live",
  );
  const [runs, setRuns] = useState<Run[]>(demoRuns),
    [selectedId, setSelectedId] = useState<string | null>("demo-0832"),
    [detail, setDetail] = useState<Run | null>(null);
  const [view, setView] = useState("runs"),
    [filter, setFilter] = useState("all"),
    [query, setQuery] = useState(""),
    [modeFilter, setModeFilter] = useState("all");
  const [loading, setLoading] = useState(false),
    [error, setError] = useState(""),
    [toast, setToast] = useState(""),
    [creating, setCreating] = useState(false),
    [busy, setBusy] = useState(false),
    [detailTab, setDetailTab] = useState("assets"),
    [mobileNav, setMobileNav] = useState(false);
  modeRef.current = demo;
  const selected = runs.find((r) => r.id === selectedId),
    current = detail?.id === selectedId ? detail : selected;
  function notify(text: string) {
    setToast(text);
  }
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 3500);
    return () => clearTimeout(timer);
  }, [toast]);
  async function load() {
    setLoading(true);
    setError("");
    try {
      const rows = await api<Run[]>("/runs");
      if (modeRef.current) return;
      setRuns(rows);
      setSelectedId((id) =>
        rows.some((r) => r.id === id) ? id : (rows[0]?.id ?? null),
      );
    } catch (e) {
      if (!modeRef.current) setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    if (demo) {
      setRuns(demoRuns);
      setSelectedId(demoRuns[0].id);
      setDetail(null);
      setError("");
      setLoading(false);
      return;
    }
    setRuns([]);
    setSelectedId(null);
    void load();
    const timer = setInterval(() => {
      if (!document.hidden) void load();
    }, 10000);
    return () => clearInterval(timer);
  }, [demo]);
  useEffect(() => {
    if (demo || !selectedId) return;
    const controller = new AbortController();
    void api<Run>(`/runs/${selectedId}`, { signal: controller.signal })
      .then(setDetail)
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => controller.abort();
  }, [demo, selectedId, runs]);
  useEffect(() => {
    function key(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        document.querySelector<HTMLInputElement>("#task-search")?.focus();
      }
      if (e.key === "Escape") setSelectedId(null);
    }
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, []);
  const visible = runs.filter(
    (r) =>
      (filter === "all" ||
        r.status === filter ||
        (filter === "running" && r.status === "queued")) &&
      (modeFilter === "all" || r.pipeline_mode === modeFilter) &&
      `${r.name || ""} ${r.product_code} ${r.id}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  async function review(action: "advance" | "reject") {
    if (!current) return;
    setBusy(true);
    try {
      if (demo) {
        setRuns((rows) =>
          rows.map((r) =>
            r.id === current.id
              ? { ...r, status: action === "advance" ? "running" : "queued" }
              : r,
          ),
        );
        notify(
          action === "advance"
            ? "演示：已通过，进入下一阶段"
            : "演示：已驳回，重新排队",
        );
      } else {
        await api(`/runs/${current.id}/${action}`, {
          method: "POST",
          body: JSON.stringify({ notes: "" }),
        });
        await load();
        notify(action === "advance" ? "已通过" : "已驳回");
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function choose(id: string) {
    setSelectedId(id);
    setDetailTab("assets");
  }
  function navigate(next: string) {
    setView(next);
    setMobileNav(false);
    setFilter(next === "home" ? "waiting_review" : "all");
  }
  const section =
    view === "home" ? "工作台" : view === "assets" ? "产品素材" : "创意生产";
  return (
    <Tooltip.Provider delayDuration={350}>
      <div className="app-shell">
        <aside className={`sidebar ${mobileNav ? "sidebar-open" : ""}`}>
          <a className="wordmark" href="/">
            crispy
            <span className="brand-period" />
          </a>
          <div className="workspace">
            <span className="workspace-avatar">C</span>
            <span>
              Crispy Workspace<small>本地工作区</small>
            </span>
          </div>
          <nav aria-label="主导航">
            {primaryNav.map(([id, label, Icon]) => (
              <button
                key={id}
                className={`nav-item ${view === id ? "active" : ""}`}
                aria-current={view === id ? "page" : undefined}
                onClick={() => navigate(id)}
              >
                <Icon size={18} />
                {label}
                {id === "runs" && (
                  <span className="nav-count">{runs.length}</span>
                )}
              </button>
            ))}
            <div className="nav-divider" />
            {(
              [
                ["/dashboard/research", "研究与记忆", BookOpen],
                ["/dashboard/calendar", "发布排期", CalendarDays],
                ["/dashboard/data", "数据", Activity],
              ] as [string, string, LucideIcon][]
            ).map(([path, label, Icon]) => (
              <a
                key={path}
                className="nav-item"
                href={`http://127.0.0.1:8000${path}`}
              >
                <Icon size={18} />
                {label}
                <ArrowUpRight className="nav-external" size={13} />
              </a>
            ))}
          </nav>
          <div className="sidebar-bottom">
            <div className="connection">
              <span className={`status-dot ${demo ? "demo-dot" : ""}`} />
              {demo ? "设计预览" : "本地 API"}
              <span className="mono">v0.1</span>
            </div>
            <a
              className="nav-item"
              href="http://127.0.0.1:8000/dashboard/agent-apis"
            >
              <Settings2 size={18} />
              设置
              <ArrowUpRight className="nav-external" size={13} />
            </a>
            <button
              className="nav-item"
              onClick={() => notify("快捷键：Ctrl / ⌘ K 搜索 · Esc 关闭详情")}
            >
              <CircleHelp size={18} />
              快捷键<span className="keycap">?</span>
            </button>
            <div className="user">
              <span className="user-avatar">V</span>
              <span>
                本地用户<small>个人工作空间</small>
              </span>
            </div>
          </div>
        </aside>
        {mobileNav && (
          <button
            className="nav-scrim"
            aria-label="关闭导航"
            onClick={() => setMobileNav(false)}
          />
        )}
        <div className="main-shell">
          <header className="topbar">
            <span className="mobile-menu">
              <IconButton
                icon={Menu}
                label="打开导航"
                onClick={() => setMobileNav(true)}
              />
            </span>
            <div className="breadcrumb">
              <span>{section}</span>
              <ChevronRight size={14} />
              <strong>
                {view === "assets"
                  ? "全部素材"
                  : view === "home"
                    ? "待办"
                    : "任务"}
              </strong>
            </div>
            <div className="top-actions">
              <label className="source-switch">
                <span className="sr-only">数据模式</span>
                <select
                  value={demo ? "demo" : "live"}
                  onChange={(e) => {
                    const value = e.target.value === "demo";
                    setDemo(value);
                    history.replaceState(
                      null,
                      "",
                      `?mode=${value ? "demo" : "live"}`,
                    );
                  }}
                >
                  <option value="demo">演示数据</option>
                  <option value="live">真实接口</option>
                </select>
              </label>
              <span className="top-separator" />
              <IconButton
                icon={RefreshCw}
                label="刷新"
                disabled={loading}
                onClick={() => (demo ? notify("演示数据已就绪") : void load())}
              />
              <Button onClick={() => setCreating(true)}>
                <Plus size={16} />
                新建任务
              </Button>
            </div>
          </header>
          <main>
            <div className="page-toolbar">
              <div className="tablist" role="group" aria-label="任务状态">
                {tabs.map(([id, label]) => (
                  <button
                    key={id}
                    aria-pressed={filter === id}
                    className={filter === id ? "tab active" : "tab"}
                    onClick={() => setFilter(id)}
                  >
                    {label}
                    <span>
                      {
                        runs.filter(
                          (r) =>
                            id === "all" ||
                            r.status === id ||
                            (id === "running" && r.status === "queued"),
                        ).length
                      }
                    </span>
                  </button>
                ))}
              </div>
              <div className="list-tools">
                <label className="search">
                  <Search size={16} />
                  <input
                    id="task-search"
                    placeholder="搜索任务、产品…"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    aria-label="搜索任务"
                  />
                  <kbd>⌘ K</kbd>
                </label>
                <label className="filter-select">
                  <ListFilter size={16} />
                  <span className="sr-only">创作模式</span>
                  <select
                    aria-label="创作模式"
                    value={modeFilter}
                    onChange={(e) => setModeFilter(e.target.value)}
                  >
                    <option value="all">全部模式</option>
                    {Object.entries(modes).map(([key, label]) => (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            </div>
            {error && (
              <div className="error-banner" role="alert">
                <span>{error} · 请确认后端运行于 8000 端口</span>
                <Button variant="ghost" onClick={() => void load()}>
                  重试
                </Button>
              </div>
            )}
            <div
              className={`workspace-content ${selectedId && view !== "assets" ? "with-detail" : ""}`}
            >
              <section
                className="list-panel"
                aria-label="任务列表"
                aria-busy={loading}
              >
                {view === "assets" ? (
                  <div className="asset-grid">
                    {visible.map((r) => (
                      <button
                        className="asset-tile"
                        key={r.id}
                        onClick={() => {
                          setView("runs");
                          choose(r.id);
                        }}
                      >
                        {r.image ? (
                          <img
                            src={r.image}
                            alt={r.name || r.product_code}
                            loading="lazy"
                          />
                        ) : (
                          <div className="asset-placeholder">
                            <Image size={32} />
                          </div>
                        )}
                        <div>
                          <strong>{r.name || r.product_code}</strong>
                          <Status value={r.status} />
                        </div>
                        <small>{modes[r.pipeline_mode]}</small>
                      </button>
                    ))}
                  </div>
                ) : (
                  <table>
                    <thead>
                      <tr>
                        <th>产品 / 任务</th>
                        <th>状态</th>
                        <th className="mode-column">创作模式</th>
                        <th className="date-column">
                          更新 <ArrowDown size={12} />
                        </th>
                        <th>
                          <span className="sr-only">打开详情</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {visible.map((r) => (
                        <tr
                          key={r.id}
                          className={selectedId === r.id ? "selected" : ""}
                        >
                          <td>
                            <button
                              className="product-cell"
                              onClick={() => choose(r.id)}
                            >
                              {r.image ? (
                                <img src={r.image} alt="" />
                              ) : (
                                <span className="product-placeholder">
                                  <Image size={19} />
                                </span>
                              )}
                              <span>
                                <strong>
                                  {r.name || r.product_code || r.id.slice(0, 8)}
                                </strong>
                                <small>
                                  {r.product_code || r.id.slice(0, 8)}
                                </small>
                              </span>
                            </button>
                          </td>
                          <td>
                            <Status value={r.status} />
                          </td>
                          <td className="mode-column">
                            <span className="mode">
                              <Image size={14} />
                              {modes[r.pipeline_mode] || r.pipeline_mode}
                            </span>
                          </td>
                          <td className="date-column">
                            <time dateTime={r.updated_at}>
                              {new Intl.DateTimeFormat("zh-CN", {
                                month: "2-digit",
                                day: "2-digit",
                                hour: "2-digit",
                                minute: "2-digit",
                                timeZone: "Asia/Shanghai",
                              }).format(new Date(r.updated_at))}
                            </time>
                          </td>
                          <td>
                            <IconButton
                              icon={ChevronRight}
                              label={`查看 ${r.name || r.product_code}`}
                              onClick={() => choose(r.id)}
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                {!visible.length && (
                  <div className="empty-state">
                    {loading ? (
                      <LoaderCircle className="spin" size={24} />
                    ) : (
                      <Search size={24} />
                    )}
                    <p>
                      {loading
                        ? "正在加载"
                        : query || modeFilter !== "all"
                          ? "没有匹配的任务"
                          : error
                            ? "连接失败"
                            : "暂无任务"}
                    </p>
                    {!loading && !error && (
                      <Button
                        variant="outline"
                        onClick={() =>
                          query || modeFilter !== "all"
                            ? (setQuery(""),
                              setModeFilter("all"),
                              setFilter("all"))
                            : setCreating(true)
                        }
                      >
                        {query || modeFilter !== "all"
                          ? "清除筛选"
                          : "新建任务"}
                      </Button>
                    )}
                  </div>
                )}
                <footer className="list-footer">
                  <span>{visible.length} 个任务</span>
                  <span>
                    {demo ? "演示数据 · 非真实执行结果" : "每 10 秒刷新"}
                  </span>
                </footer>
              </section>
              {current && view !== "assets" && (
                <aside className="detail-panel" aria-label="任务详情">
                  <div className="detail-top">
                    <span className="overline">任务详情</span>
                    <IconButton
                      icon={PanelRightClose}
                      label="关闭详情"
                      onClick={() => setSelectedId(null)}
                    />
                  </div>
                  <div className="detail-identity">
                    {current.image ? (
                      <img src={current.image} alt="" />
                    ) : (
                      <span className="product-placeholder">
                        <Image size={24} />
                      </span>
                    )}
                    <div>
                      <p className="product-title">
                        {current.name || current.product_code}
                      </p>
                      <span className="detail-code">
                        {current.product_code}
                        <IconButton
                          icon={Copy}
                          label="复制产品编号"
                          onClick={() =>
                            void navigator.clipboard
                              .writeText(current.product_code)
                              .then(
                                () => notify("已复制"),
                                () => notify("复制失败"),
                              )
                          }
                        />
                      </span>
                    </div>
                  </div>
                  <div
                    className="detail-tabs"
                    role="group"
                    aria-label="详情视图"
                  >
                    {[
                      ["assets", "素材"],
                      ["stages", "阶段"],
                      ["info", "信息"],
                    ].map(([id, label]) => (
                      <button
                        key={id}
                        className={detailTab === id ? "active" : ""}
                        aria-pressed={detailTab === id}
                        onClick={() => setDetailTab(id)}
                      >
                        {label}
                      </button>
                    ))}
                    <span className="detail-status">
                      <Status value={current.status} />
                    </span>
                  </div>
                  <div className="detail-scroll">
                    <div
                      className="stage-progress"
                      aria-label={`当前阶段：${stageNames[current.current_stage || ""] || "待执行"}`}
                    >
                      {progress(current).map(({ label, state }) => (
                        <div key={label} className={state}>
                          <span>
                            {state === "done" ? <Check size={12} /> : <span />}
                          </span>
                          <small>{label}</small>
                        </div>
                      ))}
                    </div>
                    {detailTab === "assets" ? (
                      demo ? (
                        <>
                          <div className="section-line">
                            <span>产品参考</span>
                            <span className="subtle">
                              {current.product_code}
                            </span>
                          </div>
                          <a
                            className="preview-image"
                            href={current.image}
                            target="_blank"
                            rel="noreferrer"
                            aria-label="打开完整参考图"
                          >
                            <img
                              src={current.image}
                              alt={`${current.name}产品参考图`}
                            />
                            <span className="preview-label">
                              <ExternalLink size={12} />
                              参考素材
                            </span>
                          </a>
                          <div className="section-line">
                            <span>素材预览</span>
                            <span className="subtle">演示</span>
                          </div>
                          <div className="variant-previews">
                            {["/dress.jpg", "/silver.jpg", "/robe.jpg"].map(
                              (src, i) => (
                                <button
                                  className="variant"
                                  key={src}
                                  onClick={() =>
                                    notify("演示素材，未关联生成结果")
                                  }
                                >
                                  <img
                                    src={src}
                                    alt={`演示素材 ${i + 1}`}
                                    loading="lazy"
                                  />
                                  <span>
                                    参考 {i + 1}
                                    <ExternalLink size={12} />
                                  </span>
                                </button>
                              ),
                            )}
                          </div>
                        </>
                      ) : (
                        <LiveAssets runId={current.id} />
                      )
                    ) : detailTab === "stages" ? (
                      <ol className="stage-list">
                        {(demo
                          ? plan(current).map((stage_name, i) => ({
                              id: stage_name,
                              stage_name,
                              status:
                                current.status === "completed" ||
                                i <
                                  plan(current).indexOf(
                                    current.current_stage || "",
                                  )
                                  ? "completed"
                                  : stage_name === current.current_stage
                                    ? current.status
                                    : "queued",
                              error_message: null,
                            }))
                          : current.stage_tasks || []
                        ).map((stage) => (
                          <li key={stage.id}>
                            <span>
                              {stageNames[stage.stage_name] || stage.stage_name}
                            </span>
                            <Status value={stage.status} />
                            {stage.error_message && (
                              <p role="alert">{stage.error_message}</p>
                            )}
                          </li>
                        ))}
                      </ol>
                    ) : (
                      <dl className="info-list">
                        {[
                          ["任务", current.id],
                          ["产品", current.product_code],
                          ["模式", modes[current.pipeline_mode]],
                          [
                            "当前阶段",
                            stageNames[current.current_stage || ""] || "—",
                          ],
                          ["数据", demo ? "演示" : "本地 API"],
                        ].map(([key, value]) => (
                          <div key={key}>
                            <dt>{key}</dt>
                            <dd>{value}</dd>
                          </div>
                        ))}
                      </dl>
                    )}
                  </div>
                  <div className="detail-footer">
                    <span className="subtle">
                      {stageNames[current.current_stage || ""] || "待执行"}
                    </span>
                    {current.status === "waiting_review" ? (
                      <div>
                        <IconButton
                          icon={X}
                          label="驳回阶段"
                          disabled={busy}
                          onClick={() => void review("reject")}
                        />
                        <Button
                          disabled={busy}
                          onClick={() => void review("advance")}
                        >
                          {busy ? (
                            <LoaderCircle className="spin" size={16} />
                          ) : (
                            <Check size={16} />
                          )}
                          通过
                        </Button>
                      </div>
                    ) : (
                      <Status value={current.status} />
                    )}
                  </div>
                </aside>
              )}
            </div>
          </main>
        </div>
        <CreateDialog
          open={creating}
          onOpenChange={setCreating}
          demo={demo}
          onCreated={(run) => {
            setRuns((rows) => [run, ...rows]);
            setSelectedId(run.id);
            setView("runs");
            setFilter("all");
            notify(demo ? "演示任务已创建" : "任务已创建");
          }}
        />
        {toast && (
          <div className="toast" role="status">
            <Check size={16} />
            {toast}
            <IconButton
              icon={X}
              label="关闭提示"
              onClick={() => setToast("")}
            />
          </div>
        )}
      </div>
    </Tooltip.Provider>
  );
}

function LiveAssets({ runId }: { runId: string }) {
  const [state, setState] = useState<{
    loading: boolean;
    error?: string;
    count?: number;
  }>({ loading: true });
  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true });
    api<{ variants: unknown[] }>(`/runs/${runId}/variants`, {
      signal: controller.signal,
    })
      .then((data) =>
        setState({ loading: false, count: data.variants?.length || 0 }),
      )
      .catch((e) => {
        if (e.name !== "AbortError")
          setState({ loading: false, error: e.message });
      });
    return () => controller.abort();
  }, [runId]);
  return (
    <div className="empty-state">
      <Image size={24} />
      <p>
        {state.loading
          ? "正在加载素材"
          : state.error
            ? "素材加载失败"
            : `${state.count} 个创意变体`}
      </p>
      <a
        className="button button-outline button-default"
        href="http://127.0.0.1:8000/dashboard"
      >
        打开旧版审核
        <ArrowUpRight size={14} />
      </a>
    </div>
  );
}
function CreateDialog({
  open,
  onOpenChange,
  demo,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  demo: boolean;
  onCreated: (r: Run) => void;
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setBusy(true);
    setError("");
    const payload = {
      workspace_name: String(f.get("workspace")).trim(),
      project_name: String(f.get("project")).trim(),
      product_name: String(f.get("name")).trim(),
      product_code: String(f.get("code")).trim(),
      industry_code: String(f.get("industry")).trim(),
      campaign_name: String(f.get("campaign")).trim(),
      creative_preset: String(f.get("preset")).trim(),
      pipeline_mode: String(f.get("mode")),
      approval_mode: String(f.get("approval")),
      variant_count: Number(f.get("count")),
      market: "US",
      locale: "en-US",
    };
    try {
      let run: Run;
      if (demo) {
        run = {
          id: `demo-${Date.now()}`,
          name: payload.product_name,
          product_code: payload.product_code,
          status: "draft",
          current_stage: "intake",
          pipeline_mode: payload.pipeline_mode,
          updated_at: new Date().toISOString(),
          project_id: "demo",
        };
      } else {
        const preflight = await api<{
          ok: boolean;
          severity: string;
          checks: { message: string }[];
        }>("/runs/preflight", {
          method: "POST",
          body: JSON.stringify({
            pipeline_mode: payload.pipeline_mode,
            has_image_inputs: false,
            has_video_inputs: false,
          }),
        });
        if (preflight.severity === "error")
          throw new Error(preflight.checks.map((c) => c.message).join("；"));
        run = await api<Run>("/runs", {
          method: "POST",
          body: JSON.stringify(payload),
        });
      }
      onCreated(run);
      onOpenChange(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(value) => {
        if (!busy) {
          setError("");
          onOpenChange(value);
        }
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="create-dialog" aria-describedby={undefined}>
          <div className="dialog-header">
            <Dialog.Title asChild>
              <p>新建任务</p>
            </Dialog.Title>
            <Dialog.Close asChild>
              <Button
                variant="ghost"
                size="icon"
                aria-label="关闭创建窗口"
                disabled={busy}
              >
                <X size={18} />
              </Button>
            </Dialog.Close>
          </div>
          <form onSubmit={submit}>
            <div className="form-scroll">
              <div className="form-section-label">产品</div>
              <Field label="产品名称">
                <input
                  name="name"
                  required
                  maxLength={120}
                  placeholder="例如：夏日亚麻连衣裙"
                />
              </Field>
              <div className="form-row">
                <Field label="产品编号">
                  <input
                    name="code"
                    required
                    maxLength={80}
                    placeholder="LINEN-032"
                  />
                </Field>
                <Field label="行业">
                  <input name="industry" required placeholder="apparel" />
                </Field>
              </div>
              <div className="form-section-label">工作空间</div>
              <div className="form-row">
                <Field label="店铺">
                  <input name="workspace" required placeholder="店铺名称" />
                </Field>
                <Field label="项目">
                  <input name="project" required placeholder="项目名称" />
                </Field>
              </div>
              <Field label="推广活动">
                <input name="campaign" required placeholder="活动名称" />
              </Field>
              <div className="form-section-label">创作设置</div>
              <div className="form-row">
                <Field label="创作模式">
                  <select name="mode">
                    {Object.entries(modes).map(([id, label]) => (
                      <option key={id} value={id}>
                        {label}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="审核方式">
                  <select name="approval">
                    <option value="manual">逐阶段审核</option>
                    <option value="semi_auto">创意阶段审核</option>
                    <option value="full_auto">自动执行</option>
                  </select>
                </Field>
              </div>
              <div className="form-row">
                <Field label="创意预设">
                  <input name="preset" required placeholder="预设标识" />
                </Field>
                <Field label="变体数量">
                  <input
                    name="count"
                    type="number"
                    min="1"
                    max="20"
                    defaultValue="3"
                    required
                  />
                </Field>
              </div>
              {error && (
                <div className="form-error" role="alert">
                  {error}
                </div>
              )}
            </div>
            <div className="dialog-footer">
              <span className="subtle">
                {demo ? "演示 · 不执行生成" : "本地 API"}
              </span>
              <Button type="submit" disabled={busy}>
                {busy ? (
                  <LoaderCircle className="spin" size={16} />
                ) : (
                  <Plus size={16} />
                )}
                创建任务
              </Button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
