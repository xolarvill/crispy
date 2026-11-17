import assert from "node:assert/strict";
import test from "node:test";
import { fromTwick, toTwick, newClip } from "../src/video-data.ts";

const source = {
  id: "source",
  kind: "video",
  name: "Video",
  uri: "assets/video.mp4",
  sha256: "hash",
  duration_us: 10_000_000,
  width: 640,
  height: 360,
  has_audio: true,
};
const clip = {
  ...newClip("text"),
  id: "my-title",
  text: "Title",
  start_us: 1_000_000,
  duration_us: 3_000_000,
  x: 0.08,
  y: 0.75,
  width: 0.84,
  height: 0.2,
};
const document = {
  version: 1,
  width: 1280,
  height: 720,
  fps_num: 30,
  fps_den: 1,
  background: "#111111",
  tracks: [{ id: "titles", name: "Titles", clips: [clip] }],
};

test("neutral geometry and stable IDs survive undo then redo of an absent clip", () => {
  const sdk = toTwick(document, []);
  const empty = { ...document, tracks: [] };
  const restored = fromTwick(sdk, empty, []);
  assert.deepEqual(restored, document);
});

test("SDK timing edits retain immutable source identity and geometry", () => {
  const video = {
    ...newClip("video", source),
    id: "e-video",
    duration_us: 4_000_000,
    source_in_us: 2_000_000,
    x: 0.1,
    y: 0.1,
    width: 0.8,
    height: 0.8,
  };
  const doc = {
    ...document,
    tracks: [{ id: "t-video", name: "Video", clips: [video] }],
  };
  const sdk = toTwick(doc, [source]);
  sdk.tracks[0].elements[0].s = 1;
  sdk.tracks[0].elements[0].e = 3;
  sdk.tracks[0].elements[0].props.time = 3;
  const next = fromTwick(sdk, doc, [source]).tracks[0].clips[0];
  assert.equal(next.start_us, 1_000_000);
  assert.equal(next.duration_us, 2_000_000);
  assert.equal(next.source_in_us, 3_000_000);
  assert.equal(next.source_id, source.id);
  assert.equal(next.x, 0.1);
  assert.ok(
    sdk.tracks[0].elements[0].props.src.startsWith("http://localhost/"),
  );
});

test("cloning a view clip creates a new identity rather than duplicating its source memento ID", () => {
  const sdk = toTwick(document, []);
  sdk.tracks[0].elements[0].id = "e-cloned";
  const result = fromTwick(sdk, document, []);
  assert.equal(result.tracks[0].clips[0].id, "e-cloned");
  assert.equal(result.tracks[0].clips[0].y, 0.75);
});
