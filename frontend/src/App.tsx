import { lazy, Suspense, useEffect, useState } from "react";
import {
  Activity,
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  CalendarDays,
  Check,
  ChevronRight,
  Copy,
  FolderClosed,
  Image,
  Layers,
  ListFilter,
  LoaderCircle,
  Menu,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  X,
  type LucideIcon,
} from "lucide-react";
import * as Tooltip from "@radix-ui/react-tooltip";
import { Button } from "./components/ui/button";
import { api, demoRuns, modes, plan, stageNames, type Run } from "./data";
import CreatePage from "./CreatePage";
const OperationsPage = lazy(() => import("./OperationsPage"));
const ShopsPage = lazy(() => import("./ShopsPage"));
import { AnimatedSelection, PolishedAsset } from "./components/polish";
import { IconButton, Status } from "./components/console-controls";
import { go } from "./navigation";
import {
  RunMediaGallery,
  RunMediaStrip,
  TaskMediaPanel,
  type MediaPreviewOptions,
} from "./components/run-media";
import type { RunMedia } from "./data";
function route() {
  const path = location.hash.slice(1) || "/tasks";
  return path === "/research" ? "/product-research" : path;
}
function date(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Asia/Shanghai",
  }).format(new Date(value));
}
const filters = [
  ["all", "全部"],
  ["running", "进行中"],
  ["waiting_review", "待审核"],
  ["completed", "已完成"],
];
export default function App() {
  const [preview, setPreview] = useState<{
    run: Run;
    media: RunMedia;
    trigger: HTMLElement;
    options?: MediaPreviewOptions;
  } | null>(null);
  const [path, setPath] = useState(route),
    [demo, setDemo] = useState(
      () => new URLSearchParams(location.search).get("mode") !== "live",
    );
  const [runs, setRuns] = useState<Run[]>(demo ? demoRuns : []),
    [detail, setDetail] = useState<Run | null>(null),
    [loading, setLoading] = useState(!demo),
    [detailLoading, setDetailLoading] = useState(false),
    [error, setError] = useState("");
  const [revision, setRevision] = useState(0),
    [filter, setFilter] = useState("all"),
    [query, setQuery] = useState(""),
    [modeFilter, setModeFilter] = useState("all"),
    [toast, setToast] = useState(""),
    [busy, setBusy] = useState(false),
    [mobileNav, setMobileNav] = useState(false),
    [detailTab, setDetailTab] = useState("overview");
  const operation = [
    "product-research",
    "competitor-research",
    "memory",
    "calendar",
    "data",
  ].find((name) => path === `/${name}`);
  const isShops = path.startsWith("/shops"),
    isNew = path === "/new",
    isAssets = path === "/assets",
    selectedId = path.startsWith("/tasks/")
      ? decodeURIComponent(path.slice(7))
      : null,
    current =
      detail?.id === selectedId
        ? detail
        : runs.find((r) => r.id === selectedId);
  useEffect(() => {
    const update = () => {
      setPath(route());
      setMobileNav(false);
      setError("");
      setPreview(null);
    };
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  useEffect(() => {
    document.title = `crispy · ${operation ? { "product-research": "产品研究", "competitor-research": "竞品研究", memory: "记忆库", calendar: "发布排期", data: "数据" }[operation] : isShops ? "店铺与凭证" : isNew ? "新建任务" : selectedId ? "任务详情" : isAssets ? "产品素材" : "任务"} — the marketing workbench`;
  }, [isNew, selectedId, isAssets, isShops, operation]);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 3500);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    if (demo || isShops || operation) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    api<Run[]>("/runs", { signal: controller.signal })
      .then(setRuns)
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [demo, revision, isShops, operation]);
  useEffect(() => {
    if (demo || isShops || operation) return;
    const timer = setInterval(() => {
      if (!document.hidden) setRevision((r) => r + 1);
    }, 10000);
    return () => clearInterval(timer);
  }, [demo, isShops, operation]);
  useEffect(() => {
    setDetail(null);
    setDetailTab("overview");
  }, [selectedId, demo]);
  useEffect(() => {
    if (demo || !selectedId) {
      setDetailLoading(false);
      return;
    }
    const controller = new AbortController();
    setDetailLoading(true);
    api<Run>(`/runs/${encodeURIComponent(selectedId)}`, {
      signal: controller.signal,
    })
      .then(setDetail)
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });
    return () => controller.abort();
  }, [selectedId, demo, revision]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        document.getElementById("task-search")?.focus();
      }
      if (e.key === "Escape") setMobileNav(false);
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  useEffect(() => {
    const frame = requestAnimationFrame(() =>
      document.querySelector<HTMLElement>(".page-title")?.focus(),
    );
    return () => cancelAnimationFrame(frame);
  }, [path]);
  function switchMode(value: string) {
    setPreview(null);
    const next = value === "demo";
    setDemo(next);
    setRuns(next ? demoRuns : []);
    setDetail(null);
    setError("");
    history.replaceState(
      null,
      "",
      `${location.pathname}?mode=${value}${location.hash}`,
    );
    if (isShops)
      go(path.startsWith("/shops/shared") ? "/shops/shared" : "/shops");
    else if (selectedId || isNew) go("/tasks");
  }
  function navigate(next: string) {
    setFilter("all");
    setQuery("");
    setModeFilter("all");
    go(next);
    setMobileNav(false);
  }
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
        const stages = plan(current),
          next = stages[stages.indexOf(current.current_stage || "") + 1];
        setRuns((rows) =>
          rows.map((r) =>
            r.id === current.id
              ? {
                  ...r,
                  status:
                    action === "reject"
                      ? "queued"
                      : next
                        ? "running"
                        : "completed",
                  current_stage:
                    action === "reject"
                      ? r.current_stage
                      : next || r.current_stage,
                  updated_at: new Date().toISOString(),
                }
              : r,
          ),
        );
        setToast(action === "advance" ? "演示：已通过" : "演示：已重新排队");
      } else {
        await api(`/runs/${encodeURIComponent(current.id)}/${action}`, {
          method: "POST",
          body: JSON.stringify({ notes: "" }),
        });
        setRevision((r) => r + 1);
        setToast(action === "advance" ? "已通过" : "已驳回");
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Tooltip.Provider delayDuration={300}>
      <div className="console">
        <div className="mobile-nav-toggle">
          <IconButton
            icon={mobileNav ? X : Menu}
            label={mobileNav ? "关闭导航" : "打开导航"}
            expanded={mobileNav}
            onClick={() => setMobileNav((v) => !v)}
          />
        </div>
        <aside className={`navigation ${mobileNav ? "navigation-open" : ""}`}>
          <a
            href="#/tasks"
            className="workspace-brand"
            onClick={() => navigate("/tasks")}
          >
            <span className="brand-mark">
              <Layers size={25} />
            </span>
            <span>
              <strong>crispy</strong>
            </span>
          </a>
          <div className="nav-context">
            <FolderClosed size={16} />
            工作空间
          </div>
          <div className="nav-section">营销工作</div>
          <nav aria-label="主导航">
            {(
              [
                ["/tasks", "任务", Layers],
                ["/assets", "产品素材", Image],
              ] as [string, string, LucideIcon][]
            ).map(([target, label, Icon]) => (
              <a
                href={`#${target}`}
                className={`nav-link ${(target === "/tasks" && !isAssets && !isShops && !operation) || (target === "/assets" && isAssets) ? "active" : ""}`}
                aria-current={
                  (target === "/tasks" &&
                    !isAssets &&
                    !isShops &&
                    !operation) ||
                  (target === "/assets" && isAssets)
                    ? "page"
                    : undefined
                }
                key={target}
                onClick={() => navigate(target)}
              >
                <Icon size={16} />
                {label}
              </a>
            ))}
            <div className="nav-section separated">洞察与运营</div>
            {(
              [
                ["product-research", "产品研究", Search],
                ["competitor-research", "竞品研究", ListFilter],
                ["memory", "记忆库", BookOpen],
                ["calendar", "发布排期", CalendarDays],
                ["data", "数据", Activity],
              ] as [string, string, LucideIcon][]
            ).map(([id, label, Icon]) => (
              <a
                className={`nav-link ${operation === id ? "active" : ""}`}
                aria-current={operation === id ? "page" : undefined}
                onClick={() => navigate(`/${id}`)}
                href={`#/${id}`}
                key={id}
              >
                <Icon size={16} />
                {label}
              </a>
            ))}
          </nav>
          <div className="nav-bottom">
            <a
              className={`nav-link ${isShops ? "active" : ""}`}
              aria-current={isShops ? "page" : undefined}
              href="#/shops"
              onClick={() => navigate("/shops")}
            >
              <Settings2 size={16} />
              店铺与凭证
            </a>
            <label className="data-mode sidebar-data-mode">
              <span className="sr-only">数据模式</span>
              <select
                value={demo ? "demo" : "live"}
                onChange={(e) => switchMode(e.target.value)}
              >
                <option value="demo">演示数据</option>
                <option value="live">本地 API</option>
              </select>
            </label>
            <div className="environment">
              <span className="connection-dot" />
              {demo ? "设计预览" : "本地 API"}
              <span>v0.3</span>
            </div>
          </div>
        </aside>
        {mobileNav && (
          <button
            className="nav-backdrop"
            aria-label="关闭导航"
            onClick={() => setMobileNav(false)}
          />
        )}
        <main className="console-main">
          <div className={`page ${isNew ? "creation-page" : ""}`}>
            {!isShops && !operation && (
              <div className="breadcrumb">
                <a href="#/tasks" onClick={() => navigate("/tasks")}>
                  营销工作
                </a>
                <ChevronRight size={12} />
                {selectedId || isNew ? (
                  <>
                    <a href="#/tasks">任务</a>
                    <ChevronRight size={12} />
                    <span>
                      {isNew ? "新建" : current?.product_code || selectedId}
                    </span>
                  </>
                ) : (
                  <span>{isAssets ? "产品素材" : "任务"}</span>
                )}
              </div>
            )}
            {error && !isShops && (
              <div className="alert" role="alert">
                <span>{error} · 后端地址 127.0.0.1:8000</span>
                <Button
                  variant="outline"
                  onClick={() => setRevision((r) => r + 1)}
                >
                  <RefreshCw size={14} />
                  重试
                </Button>
              </div>
            )}
            {operation ? (
              <Suspense fallback={<p role="status">加载中…</p>}>
                <OperationsPage
                  key={`${operation}-${demo}`}
                  section={operation}
                  demo={demo}
                />
              </Suspense>
            ) : isShops ? (
              <Suspense
                fallback={
                  <div className="empty-state">
                    <LoaderCircle size={24} className="spin" />
                    <p>正在加载店铺凭证</p>
                  </div>
                }
              >
                <ShopsPage
                  key={demo ? "demo" : "live"}
                  path={path}
                  demo={demo}
                  notify={setToast}
                />
              </Suspense>
            ) : isNew ? (
              <CreatePage
                demo={demo}
                onCreated={(run) => {
                  setRuns((rows) => [run, ...rows]);
                  setFilter("all");
                  setQuery("");
                  setToast(demo ? "演示任务已创建" : "任务已创建");
                  go(`/tasks/${run.id}`);
                }}
              />
            ) : selectedId ? (
              <>
                <div className="resource-header task-resource-header">
                  <div className="resource-name">
                    <IconButton
                      icon={ArrowLeft}
                      label="返回任务列表"
                      onClick={() => go("/tasks")}
                    />
                    <p className="page-title" tabIndex={-1}>
                      {current?.name || current?.product_code || "任务详情"}
                    </p>
                    {current && <Status value={current.status} />}
                  </div>
                  {current && (
                    <div className="resource-summary">
                      <span>
                        <small>当前阶段</small>
                        {stageNames[current.current_stage || ""] || "待执行"}
                      </span>
                      <span>
                        <small>创作模式</small>
                        {modes[current.pipeline_mode]}
                      </span>
                      <span>
                        <small>产品编号</small>
                        {current.product_code}
                      </span>
                      <span>
                        <small>更新</small>
                        {date(current.updated_at)}
                      </span>
                    </div>
                  )}
                  <div className="resource-actions">
                    <IconButton
                      icon={Copy}
                      label="复制任务编号"
                      onClick={() =>
                        void navigator.clipboard.writeText(selectedId).then(
                          () => setToast("已复制任务编号"),
                          () => setToast("复制失败"),
                        )
                      }
                    />
                    <IconButton
                      icon={RefreshCw}
                      label="刷新任务"
                      onClick={() =>
                        demo
                          ? setToast("当前为演示数据")
                          : setRevision((r) => r + 1)
                      }
                    />
                  </div>
                </div>
                <div
                  className="resource-tabs"
                  role="group"
                  aria-label="任务视图"
                >
                  <AnimatedSelection active={detailTab}>
                    {[
                      ["overview", "概览"],
                      ["stages", "执行阶段"],
                      ["info", "任务信息"],
                    ].map(([id, label]) => (
                      <button
                        key={id}
                        data-selection-id={id}
                        aria-pressed={detailTab === id}
                        className={detailTab === id ? "active" : ""}
                        onClick={() => setDetailTab(id)}
                      >
                        {label}
                      </button>
                    ))}
                  </AnimatedSelection>
                </div>
                {current ? (
                  <>
                    {detailTab === "overview" ? (
                      <div className="review-workspace">
                        <TaskMediaPanel
                          key={current.id}
                          run={current}
                          demo={demo}
                          refreshKey={revision}
                          open={(run, media, trigger, options) =>
                            setPreview({ run, media, trigger, options })
                          }
                        />
                        <section
                          className="execution-section"
                          aria-label="执行记录"
                        >
                          <div className="section-toolbar">
                            <p>执行阶段</p>
                            <span className="muted">
                              {plan(current).length} 阶段
                            </span>
                          </div>
                          <StageList run={current} demo={demo} />
                          <div className="review-action">
                            <div>
                              <Status value={current.status} />
                              <span className="muted">
                                {stageNames[current.current_stage || ""]}
                              </span>
                            </div>
                            {current.status === "waiting_review" && !demo ? (
                              <a
                                className="button button-primary button-default"
                                href="http://127.0.0.1:8000/dashboard"
                              >
                                打开完整审核
                                <ArrowUpRight size={14} />
                              </a>
                            ) : current.status === "waiting_review" ? (
                              <div className="action-buttons">
                                <Button
                                  variant="outline"
                                  disabled={busy}
                                  onClick={() => void review("reject")}
                                >
                                  <X size={15} />
                                  驳回
                                </Button>
                                <Button
                                  disabled={busy}
                                  onClick={() => void review("advance")}
                                >
                                  {busy ? (
                                    <LoaderCircle size={15} className="spin" />
                                  ) : (
                                    <Check size={15} />
                                  )}
                                  通过阶段
                                </Button>
                              </div>
                            ) : (
                              <span className="muted">
                                {demo ? "演示状态" : "每 10 秒刷新"}
                              </span>
                            )}
                          </div>
                        </section>
                      </div>
                    ) : detailTab === "stages" ? (
                      <section className="surface stage-page">
                        <StageList run={current} demo={demo} />
                      </section>
                    ) : (
                      <section className="surface info-page">
                        <dl>
                          {[
                            ["任务编号", current.id],
                            ["产品编号", current.product_code],
                            ["项目", current.project_id],
                            ["创作模式", modes[current.pipeline_mode]],
                            ["数据来源", demo ? "演示" : "本地 API"],
                          ].map(([label, value]) => (
                            <div key={label}>
                              <dt>{label}</dt>
                              <dd>{value}</dd>
                            </div>
                          ))}
                        </dl>
                      </section>
                    )}
                  </>
                ) : (
                  <div className="empty-state">
                    {detailLoading ? (
                      <LoaderCircle className="spin" size={24} />
                    ) : (
                      <FolderClosed size={24} />
                    )}
                    <p>{detailLoading ? "正在加载" : "找不到该任务"}</p>
                    <Button variant="outline" onClick={() => go("/tasks")}>
                      返回列表
                    </Button>
                  </div>
                )}
              </>
            ) : (
              <>
                <div className="resource-header">
                  <p className="page-title" tabIndex={-1}>
                    {isAssets ? "产品素材" : "任务"}
                  </p>
                  <Button onClick={() => go("/new")}>
                    <Plus size={15} />
                    新建任务
                  </Button>
                </div>
                <div
                  className="resource-tabs"
                  role="group"
                  aria-label="任务状态"
                >
                  <AnimatedSelection active={filter}>
                    {filters.map(([id, label]) => (
                      <button
                        key={id}
                        data-selection-id={id}
                        aria-pressed={filter === id}
                        className={filter === id ? "active" : ""}
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
                  </AnimatedSelection>
                </div>
                <section
                  className={isAssets ? "material-index" : "surface task-index"}
                  aria-label={isAssets ? "产品素材列表" : "任务列表"}
                  aria-busy={loading}
                >
                  <div className="index-toolbar">
                    <label className="search">
                      <Search size={16} />
                      <input
                        id="task-search"
                        aria-label="搜索任务"
                        placeholder="搜索名称或产品编号"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                      />
                      <kbd>⌘ K</kbd>
                    </label>
                    <div className="toolbar-right">
                      <label className="mode-filter">
                        <ListFilter size={14} />
                        <span className="sr-only">创作模式</span>
                        <select
                          value={modeFilter}
                          onChange={(e) => setModeFilter(e.target.value)}
                        >
                          <option value="all">全部模式</option>
                          {Object.entries(modes).map(([id, label]) => (
                            <option key={id} value={id}>
                              {label}
                            </option>
                          ))}
                        </select>
                      </label>
                      <IconButton
                        icon={RefreshCw}
                        label="刷新列表"
                        disabled={loading}
                        onClick={() =>
                          demo
                            ? setToast("当前为演示数据")
                            : setRevision((r) => r + 1)
                        }
                      />
                    </div>
                  </div>
                  {isAssets ? (
                    <div className="asset-grid">
                      {visible.map((r) => (
                        <PolishedAsset href={`#/tasks/${r.id}`} key={r.id}>
                          {r.image ? (
                            <img
                              src={r.image}
                              alt={r.name || r.product_code}
                              loading="lazy"
                            />
                          ) : (
                            <div className="asset-placeholder">
                              <Image size={24} />
                            </div>
                          )}
                          <div>
                            <strong>{r.name || r.product_code}</strong>
                            <ArrowUpRight size={14} />
                          </div>
                          <small>{r.product_code} · 参考素材</small>
                        </PolishedAsset>
                      ))}
                    </div>
                  ) : (
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>任务 / 产品</th>
                            <th className="task-media-column">媒体</th>
                            <th>状态</th>
                            <th className="mode-column">创作模式</th>
                            <th className="stage-column">当前阶段</th>
                            <th className="date-column">
                              更新 <ArrowDown size={12} />
                            </th>
                            <th>
                              <span className="sr-only">打开任务</span>
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {visible.map((r) => (
                            <tr key={r.id}>
                              <td>
                                <a
                                  className="task-link"
                                  href={`#/tasks/${r.id}`}
                                >
                                  <strong>
                                    {r.name ||
                                      r.product_code ||
                                      r.id.slice(0, 8)}
                                  </strong>
                                  <small>
                                    {r.product_code || r.id.slice(0, 8)}
                                  </small>
                                </a>
                              </td>
                              <td className="task-media-column">
                                <RunMediaStrip
                                  run={r}
                                  demo={demo}
                                  open={(run, media, trigger) =>
                                    setPreview({ run, media, trigger })
                                  }
                                />
                              </td>
                              <td>
                                <Status value={r.status} />
                              </td>
                              <td className="mode-column">
                                {modes[r.pipeline_mode] || r.pipeline_mode}
                              </td>
                              <td className="stage-column">
                                {stageNames[r.current_stage || ""] || "待执行"}
                              </td>
                              <td className="date-column">
                                <time dateTime={r.updated_at}>
                                  {date(r.updated_at)}
                                </time>
                              </td>
                              <td>
                                <IconButton
                                  icon={ArrowRight}
                                  label={`查看 ${r.name || r.product_code}`}
                                  onClick={() => go(`/tasks/${r.id}`)}
                                />
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
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
                          : error
                            ? "连接失败"
                            : query || modeFilter !== "all" || filter !== "all"
                              ? "没有匹配的任务"
                              : "暂无任务"}
                      </p>
                      {!loading && !error && (
                        <Button
                          variant="outline"
                          onClick={() => {
                            setQuery("");
                            setModeFilter("all");
                            setFilter("all");
                          }}
                        >
                          清除筛选
                        </Button>
                      )}
                    </div>
                  )}
                  <div className="index-footer">
                    <span>{visible.length} 个任务</span>
                    <span>
                      {demo ? "演示数据 · 非真实执行结果" : "每 10 秒刷新"}
                    </span>
                  </div>
                </section>
              </>
            )}
          </div>
        </main>
        {preview && (
          <RunMediaGallery
            key={preview.run.id}
            run={preview.run}
            initial={preview.media}
            trigger={preview.trigger}
            demo={demo}
            complete={preview.options?.complete}
            onSelect={preview.options?.onSelect}
            close={() => setPreview(null)}
          />
        )}
        {toast && (
          <div className="toast" role="status">
            <Check size={15} />
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
function StageList({ run, demo }: { run: Run; demo: boolean }) {
  const stages = plan(run),
    active = stages.indexOf(run.current_stage || "");
  const items = demo
    ? stages.map((stage_name, i) => ({
        id: stage_name,
        stage_name,
        status:
          run.status === "completed" || (active >= 0 && i < active)
            ? "completed"
            : i === active
              ? run.status
              : "not_started",
        error_message: null,
      }))
    : run.stage_tasks || [];
  return (
    <ol className="stage-list">
      {items.map((stage, i) => (
        <li
          key={stage.id}
          className={stage.stage_name === run.current_stage ? "current" : ""}
        >
          <span
            className={`stage-symbol ${stage.status === "completed" ? "done" : ""}`}
          >
            {stage.status === "completed" ? (
              <Check size={13} />
            ) : (
              <span>{i + 1}</span>
            )}
          </span>
          <span className="stage-content">
            <span>{stageNames[stage.stage_name] || stage.stage_name}</span>
            {stage.error_message && (
              <small className="error-text">{stage.error_message}</small>
            )}
          </span>
          <Status value={stage.status} />
        </li>
      ))}
      {!items.length && <li className="muted">暂无执行记录</li>}
    </ol>
  );
}
