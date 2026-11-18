import assert from "node:assert/strict";
import test from "node:test";
import {
  appendTrack, clipTimingLimits, cutClip, frameTimeUs, fromTwick, newClip, removeClip, toTwick,
} from "../src/video-data.ts";

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

const footage = (id, start_us, duration_us, source_in_us = 0) => ({
  ...newClip("video", source), id, start_us, duration_us, source_in_us,
});

test("trim and split at the playhead preserve source time and ignore the exact clip edges", () => {
  const video = footage("shot", 1_000_000, 4_000_000, 2_000_000);
  const doc = { ...document, fps_num: 30000, fps_den: 1001,
    tracks: [{ id: "picture", name: "Picture", clips: [video] }] };
  const time = frameTimeUs(doc, 2.01);
  assert.equal(time, 2_002_000);
  const [left, right] = cutClip(doc, video, time, "split").tracks[0].clips;
  assert.equal(left.duration_us, 1_002_000);
  assert.equal(right.source_in_us, 3_002_000);
  assert.equal(right.start_us + right.duration_us, 5_000_000);
  assert.notEqual(left.id, right.id);
  assert.deepEqual(cutClip(doc, video, time, "trim-start").tracks[0].clips,
    [{ ...right, id: video.id }]);
  assert.deepEqual(cutClip(doc, video, time, "trim-end").tracks[0].clips, [left]);
  assert.equal(cutClip(doc, video, 5_000_000, "trim-end"), doc);
  assert.equal(doc.tracks[0].clips[0].duration_us, 4_000_000);
});

test("inspector timing respects both neighbours and remaining media", () => {
  const video = footage("shot", 2_000_000, 3_000_000, 6_000_000);
  const previous = footage("before", 0, 1_500_000);
  const next = footage("after", 5_500_000, 1_000_000);
  assert.deepEqual(clipTimingLimits(video, previous, next, source), {
    minStart: 1_500_000, maxStart: 2_500_000, maxDuration: 3_500_000, maxSourceIn: 7_000_000,
  });
  assert.equal(clipTimingLimits(video, previous, undefined, source).maxDuration, 4_000_000);
});

test("closing a gap moves only subsequent clips on the selected track", () => {
  const first = footage("first", 0, 2_000_000);
  const second = footage("second", 2_500_000, 1_000_000);
  const picture = { id: "t-picture-main", name: "Picture", clips: [first, second] };
  const doc = { ...document, tracks: [picture, ...document.tracks] };
  const result = removeClip(doc, first, true);
  assert.equal(result.tracks[0].clips[0].start_us, 500_000);
  assert.deepEqual(result.tracks[1], document.tracks[0]);
  assert.equal(removeClip(doc, first, false).tracks[0].clips[0].start_us, 2_500_000);
  assert.deepEqual(removeClip(result, result.tracks[0].clips[0], false).tracks, document.tracks);
});

test("new picture clips avoid overlay and audio tracks", () => {
  const overlay = { id: "old-overlay", name: "Image", clips: [{ ...newClip("image"), width: 0.3 }] };
  const markedOverlay = { id: "t-overlay-one", name: "Image", clips: [newClip("image")] };
  const picture = { id: "t-picture-main", name: "Picture", clips: [footage("video", 0, 1_000_000)] };
  const audio = { id: "audio", name: "Audio", clips: [newClip("audio")] };
  const doc = { ...document, tracks: [overlay, markedOverlay, audio, picture] };
  assert.equal(appendTrack(doc, "image"), picture);
  assert.equal(appendTrack(doc, "audio"), audio);
  assert.equal(appendTrack({ ...doc, tracks: [overlay, markedOverlay] }, "video"), undefined);
});
