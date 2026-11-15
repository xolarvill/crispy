import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { ArrowRight, Check, ChevronDown } from "lucide-react";
import {
  stageItems,
  stageLabel,
  statusLabel,
  type Run,
} from "../data";
import { useI18n } from "../i18n";

const doneStatuses = ["completed", "approved"];

function toneOf(status: string) {
  if (doneStatuses.includes(status)) return "done";
  if (["failed", "rejected"].includes(status)) return "failed";
  if (status === "waiting_review") return "review";
  if (status === "running") return "running";
  if (status === "queued") return "queued";
  return "idle";
}

export function RunStatusPill({
  run,
  demo,
  onOpenStages,
}: {
  run: Run;
  demo: boolean;
  onOpenStages?: () => void;
}) {
  const { locale, t } = useI18n();
  const [hovered, setHovered] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [shift, setShift] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const popover = useRef<HTMLDivElement>(null);
  const panelId = useId();
  const stages = stageItems(run, demo);
  const done = stages.filter((stage) => doneStatuses.includes(stage.status)).length;
  const total = stages.length;
  const percent = total ? Math.round((done / total) * 100) : 0;
  const tone = toneOf(run.status);
  const open = hovered || pinned;
  const close = () => {
    setHovered(false);
    setPinned(false);
  };

  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const pill = root.current?.getBoundingClientRect();
      const width = popover.current?.offsetWidth;
      if (!pill || !width) return;
      const margin = 12,
        left = Math.min(
          Math.max(pill.left, margin),
          Math.max(margin, window.innerWidth - margin - width),
        );
      setShift(left - pill.left);
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    const onPointer = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) close();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [open]);

  return (
    <div
      className="run-status"
      ref={root}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setHovered(true)}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null))
          setHovered(false);
      }}
    >
      <button
        type="button"
        className={`run-status-pill tone-${tone}`}
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={t(
          `${statusLabel(run.status, locale) || run.status}, ${done} of ${total} stages complete`,
          `${statusLabel(run.status, locale) || run.status}，已完成 ${done} / ${total} 个阶段`,
        )}
        onClick={() => setPinned((value) => !value)}
      >
        <span className="run-status-spark" aria-hidden="true" />
        <span className="run-status-value">
          {statusLabel(run.status, locale) || run.status}
        </span>
        <span className="run-status-count" aria-hidden="true">
          {done}/{total}
        </span>
        <ChevronDown size={13} className="run-status-chevron" />
      </button>
      <div
        id={panelId}
        ref={popover}
        style={{ left: shift }}
        className={`run-status-popover${open ? " open" : ""}`}
        role="group"
        aria-label={t("Stage overview", "阶段总览")}
      >
        <div className="run-status-head">
          <span>{t("Stage overview", "阶段总览")}</span>
          <span className="run-status-head-count">
            {done} / {total}
          </span>
        </div>
        <div
          className="run-status-progress"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent}
          aria-label={t("Stage progress", "阶段进度")}
        >
          <span style={{ width: `${percent}%` }} />
        </div>
        <ol className="run-status-stages">
          {stages.map((stage, index) => {
            const current = stage.stage_name === run.current_stage;
            const stageTone = toneOf(stage.status);
            return (
              <li
                key={stage.id}
                data-tone={stageTone}
                className={current ? "current" : ""}
              >
                <span className="run-status-index" aria-hidden="true">
                  {stageTone === "done" ? <Check size={11} /> : index + 1}
                </span>
                <span className="run-status-name">
                  {stageLabel(stage.stage_name, locale) || stage.stage_name}
                  {current && (
                    <small>{t("Current stage", "当前阶段")}</small>
                  )}
                </span>
                <span className="run-status-tag">
                  {statusLabel(stage.status, locale) || stage.status}
                </span>
              </li>
            );
          })}
          {!stages.length && (
            <li className="run-status-empty">
              {t("No execution log yet", "暂无执行记录")}
            </li>
          )}
        </ol>
        {onOpenStages && (
          <button
            type="button"
            className="run-status-link"
            onClick={() => {
              close();
              onOpenStages();
            }}
          >
            {t("Open stage log", "打开阶段记录")}
            <ArrowRight size={13} />
          </button>
        )}
      </div>
    </div>
  );
}
