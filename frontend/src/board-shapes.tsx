/**
 * Canvas view of the instruction board.
 *
 * Only two document node types need custom shape utils: run assets (a pinned
 * image marked ①) and constrained stages. Regions are box shapes parented to
 * their asset, so dragging an asset keeps its boxes attached. Notes, text and
 * freehand drawings use tldraw's own shapes and are mapped back to plain
 * document nodes when the board is saved.
 */
import {
  BaseBoxShapeTool,
  BaseBoxShapeUtil,
  HTMLContainer,
  T,
  type Editor,
  type TLShape,
} from "tldraw";
import { mediaUrl, circledMark, newBoardId, type BoardRole } from "./boards";
import { currentLocale } from "./i18n";

export const ASSET_SHAPE = "board-asset" as const;
export const REGION_SHAPE = "board-region" as const;
export const STAGE_SHAPE = "board-stage" as const;
export const REGION_TOOL = "board-region-tool" as const;

export type BoardMeta = {
  boardId?: string;
  boardKind?: "asset" | "region" | "stage" | "note" | "text" | "sketch" | "frame";
  mediaAssetId?: string;
  mark?: number;
  role?: BoardRole;
  caption?: string;
  uri?: string;
  linkId?: string;
  linkKind?: "annotation" | "data";
  linkPort?: string;
  linkDataType?: string | null;
};

export function metaOf(shape: TLShape): BoardMeta {
  return (shape.meta || {}) as BoardMeta;
}

export function boardIdOf(shape: TLShape): string | null {
  const value = metaOf(shape).boardId;
  return typeof value === "string" && value ? value : null;
}

export function boardKindOf(shape: TLShape): BoardMeta["boardKind"] {
  return metaOf(shape).boardKind;
}

export const assetShapeProps = {
  w: T.number,
  h: T.number,
  uri: T.string,
  assetId: T.string,
  mark: T.number,
  role: T.string,
  caption: T.string,
  mediaKind: T.string,
};

export type BoardAssetShapeProps = {
  w: number;
  h: number;
  uri: string;
  assetId: string;
  mark: number;
  role: BoardRole;
  caption: string;
  mediaKind: "image" | "video";
};

export type BoardRegionShapeProps = {
  w: number;
  h: number;
  assetId: string;
  note: string;
};

export type BoardStageShapeProps = {
  w: number;
  h: number;
  stage: string;
};

declare module "tldraw" {
  interface TLGlobalShapePropsMap {
    "board-asset": BoardAssetShapeProps;
    "board-region": BoardRegionShapeProps;
    "board-stage": BoardStageShapeProps;
  }
}

export type BoardAssetShape = TLShape<typeof ASSET_SHAPE>;
export type BoardRegionShape = TLShape<typeof REGION_SHAPE>;
export type BoardStageShape = TLShape<typeof STAGE_SHAPE>;

export class BoardAssetShapeUtil extends BaseBoxShapeUtil<BoardAssetShape> {
  static override type = ASSET_SHAPE;
  static override props = assetShapeProps;

  getDefaultProps(): BoardAssetShape["props"] {
    return {
      w: 320,
      h: 400,
      uri: "",
      assetId: "",
      mark: 1,
      role: "source",
      caption: "",
      mediaKind: "image",
    };
  }

  component(shape: BoardAssetShape) {
    const { w, h, uri, mark, caption, mediaKind } = shape.props;
    return (
      <HTMLContainer className="board-asset-shape" style={{ width: w, height: h }}>
        <div className="board-asset-frame">
          {mediaKind === "video" ? (
            <video
              className="board-asset-media"
              src={mediaUrl(uri)}
              muted
              loop
              playsInline
              draggable={false}
            />
          ) : (
            <img
              className="board-asset-media"
              src={mediaUrl(uri)}
              alt={caption || `asset ${mark}`}
              draggable={false}
            />
          )}
        </div>
        <span className="board-asset-mark">{circledMark(mark)}</span>
        {caption ? <span className="board-asset-caption">{caption}</span> : null}
      </HTMLContainer>
    );
  }

  getIndicatorPath(shape: BoardAssetShape) {
    const path = new Path2D();
    path.roundRect(0, 0, shape.props.w, shape.props.h, 10);
    return path;
  }


  override getShapeName(shape: BoardAssetShape) {
    return circledMark(shape.props.mark);
  }

  // Assets are evidence, not free-form drawing: resizing is allowed, rotating
  // is not, so region boxes and arrow anchors stay meaningful.
  override canResize() {
    return true;
  }
}

export const regionShapeProps = {
  w: T.number,
  h: T.number,
  assetId: T.string,
  note: T.string,
};

export class BoardRegionShapeUtil extends BaseBoxShapeUtil<BoardRegionShape> {
  static override type = REGION_SHAPE;
  static override props = regionShapeProps;

  getDefaultProps(): BoardRegionShape["props"] {
    return { w: 160, h: 120, assetId: "", note: "" };
  }

  component(shape: BoardRegionShape) {
    const empty = !shape.props.note.trim();
    return (
      <HTMLContainer
        className={`board-region-shape${empty ? " is-empty" : ""}`}
        style={{ width: shape.props.w, height: shape.props.h }}
      >
        <span className="board-region-label">
          {empty
            ? currentLocale() === "zh"
              ? "待填写说明"
              : "Add a note"
            : shape.props.note}
        </span>
      </HTMLContainer>
    );
  }

  getIndicatorPath(shape: BoardRegionShape) {
    const path = new Path2D();
    path.rect(0, 0, shape.props.w, shape.props.h);
    return path;
  }

  // Regions are sub-areas of an asset; aligning to them would fight the
  // asset-level guides, so they never become snap targets themselves.
  override canSnap() {
    return false;
  }
}

export const stageShapeProps = {
  w: T.number,
  h: T.number,
  stage: T.string,
};

export class BoardStageShapeUtil extends BaseBoxShapeUtil<BoardStageShape> {
  static override type = STAGE_SHAPE;
  static override props = stageShapeProps;

  getDefaultProps(): BoardStageShape["props"] {
    return { w: 240, h: 120, stage: "" };
  }

  component(shape: BoardStageShape) {
    return (
      <HTMLContainer
        className="board-stage-shape"
        style={{ width: shape.props.w, height: shape.props.h }}
      >
        <span className="board-stage-kicker">
          {currentLocale() === "zh" ? "阶段" : "Stage"}
        </span>
        <span className="board-stage-name">{shape.props.stage}</span>
      </HTMLContainer>
    );
  }

  getIndicatorPath(shape: BoardStageShape) {
    const path = new Path2D();
    path.roundRect(0, 0, shape.props.w, shape.props.h, 12);
    return path;
  }
}

export const boardShapeUtils = [
  BoardAssetShapeUtil,
  BoardRegionShapeUtil,
  BoardStageShapeUtil,
];

function notify(message: string) {
  window.dispatchEvent(
    new CustomEvent("crispy-board-notice", { detail: message }),
  );
}

function assetUnderPoint(editor: Editor, point: { x: number; y: number }) {
  const candidates = editor
    .getCurrentPageShapes()
    .filter((shape) => shape.type === ASSET_SHAPE)
    .map((shape) => ({
      shape,
      bounds: editor.getShapePageBounds(shape),
    }))
    .filter((entry) => entry.bounds && entry.bounds.containsPoint(point))
    .sort((a, b) => (b.bounds?.width ?? 0) * (b.bounds?.height ?? 0) - (a.bounds?.width ?? 0) * (a.bounds?.height ?? 0));
  return candidates[0]?.shape ?? null;
}

/** Board node ids are created with the shape; older assets created before the
 * first save may not have one yet, so assign it on demand. */
function ensureBoardId(editor: Editor, shape: TLShape) {
  const existing = boardIdOf(shape);
  if (existing) return existing;
  const id = newBoardId("a");
  editor.updateShape({
    id: shape.id,
    type: shape.type,
    meta: { ...shape.meta, boardKind: "asset", boardId: id },
  });
  return id;
}

/** Drag-to-mark tool: the box must be drawn on top of an asset. */
export class BoardRegionTool extends BaseBoxShapeTool {
  static override id = REGION_TOOL;
  static override initial = "idle";
  override shapeType = REGION_SHAPE;

  override onCreate(shape: TLShape | null): void {
    if (!shape) return;
    const editor = this.editor;
    const bounds = editor.getShapePageBounds(shape);
    if (!bounds) return;
    const center = bounds.center;
    const asset = assetUnderPoint(editor, center);
    if (!asset) {
      editor.deleteShapes([shape.id]);
      notify(
        currentLocale() === "zh"
          ? "请把方框画在某个素材上面"
          : "Draw the box on top of an asset",
      );
      return;
    }
    const assetBoardId = ensureBoardId(editor, asset);
    editor.updateShape({
      id: shape.id,
      type: REGION_SHAPE,
      meta: { ...shape.meta, boardKind: "region", boardId: newBoardId("r") },
      props: { assetId: assetBoardId, note: "" },
    });
    editor.reparentShapes([shape.id], asset.id);
    editor.select(shape.id);
    window.dispatchEvent(
      new CustomEvent("crispy-board-focus-region", { detail: shape.id }),
    );
  }
}

export const boardTools = [BoardRegionTool];

export function assetShapeIdForBoardId(editor: Editor, boardId: string) {
  return (
    editor
      .getCurrentPageShapes()
      .find(
        (shape) =>
          boardKindOf(shape) === "asset" && boardIdOf(shape) === boardId,
      )?.id ?? null
  );
}

