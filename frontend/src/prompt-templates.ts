import type { Localized } from "./i18n";

export type PromptTemplate = {
  title: Localized;
  use: Localized;
  prompt: Localized;
};

export const promptTemplates: PromptTemplate[] = [
  {
    title: { en: "Product hero", zh: "商品主图" },
    use: {
      en: "Light catalog shot · product fully legible",
      zh: "浅底目录照 · 完整识别商品",
    },
    prompt: {
      en: "Shoot a square catalog photo of the wooden-handled shoe brush in the reference image. The handle runs from lower left to upper right, and the camera looks from slightly above the tabletop so both the handle end and bristle length read clearly. Keep the pale gray background flat and continuous, with soft light from one side picking out the wood grain and a narrow contact shadow under the bristles. Frame the whole product with crop margin on every side. Keep the shape, color and existing markings from the reference, and show only this one brush.",
      zh: "为参考图中的木柄鞋刷拍一张方形商品目录照。刷柄从左下向右上延伸，镜头从略高于桌面的角度看过去，同时看清柄尾轮廓和刷毛长度。浅灰背景平整连续，一侧柔光勾出木纹，刷毛下方保留细窄接触阴影。整件商品入镜，四周留出裁切余量。照参考图保留形状、颜色和已有标记，画面只出现这一把鞋刷。",
    },
  },
  {
    title: { en: "In use", zh: "功能演示" },
    use: {
      en: "Action close-up · show how it works",
      zh: "动作近景 · 看清产品如何工作",
    },
    prompt: {
      en: "Shoot a vertical close-up of the wooden-handled shoe brush touching a leather shoe. The upper fills the lower half of the frame, a hand grips the handle from the right, and the camera sits nearly level with the contact surface. Focus where the bristle tips bend slightly along the leather's curve, and use side light to separate the black bristles from the dark leather. Keep a clear stretch of handle and toe outline so the action is readable. Reproduce the bristle-to-handle join from the product reference and keep the background quiet.",
      zh: "拍一张木柄鞋刷接触皮鞋的竖幅近景。鞋面占画面下半部，手从右侧握住刷柄，镜头与接触面近乎齐平。对焦在毛尖顺着皮革曲面轻微弯曲的位置，让侧光分开黑色刷毛与深色皮面。保留一段清晰的刷柄和鞋头轮廓，让读者能认出动作。依照产品参考图还原刷毛与柄身的连接方式，背景保持安静。",
    },
  },
  {
    title: { en: "Material close-up", zh: "材质特写" },
    use: {
      en: "Detail photography · texture and craft",
      zh: "局部摄影 · 呈现触感和工艺",
    },
    prompt: {
      en: "Shoot a material detail of a plain ceramic cup. Let the rim arc cross the upper half of the frame and leave a continuous field of glaze across the front, with focus on where the lip meets the wall. Place a broad, soft light from behind and to the side, using the highlight-to-shadow transition to describe the curvature. Choose a low-contrast neighboring tone of the glaze for the background beyond the cup. Show real texture, color and rim thickness from the reference, emphasizing only the craft details that can be recognized from the object itself.",
      zh: "为一只素色陶杯拍材质细节图。让杯口弧线穿过画面上半部，正面留出一片连续釉面，焦点放在杯沿与杯壁交界处。从侧后方打一道宽柔光，用高光向暗部的过渡表现曲率。杯外背景取釉色的低对比邻近色。按照参考图呈现真实纹理、色泽和边缘厚度，只突出能够从原物辨认的工艺细节。",
    },
  },
  {
    title: { en: "In context", zh: "使用场景" },
    use: {
      en: "Lifestyle photography · space and scale",
      zh: "生活方式摄影 · 交代空间和尺度",
    },
    prompt: {
      en: "Shoot a canvas tote bag in an entryway. The bag rests on the edge of a low stool while one hand lifts one strap; the other strap hangs naturally. Set the camera close to the stool's surface so the relationship between bag base, strap and hand is clear. Morning light from the doorway grazes across the canvas, and the background keeps only an out-of-focus door frame. Match the weave, bag shape and existing print to the reference, let the hand provide scale, and add no promotional text to the frame.",
      zh: "拍一张玄关里的帆布托特包。包放在矮凳边沿，一只手正提起其中一侧提带；另一侧自然垂下。用与凳面接近的机位拍摄，让包底、提带和手之间的位置关系清晰。门外的晨光沿布面擦过，后景只保留虚焦的门框。以参考图为准还原织纹、包型与已有印花，让手提供尺度，画面里不加促销文字。",
    },
  },
  {
    title: { en: "Social cover", zh: "社媒封面" },
    use: {
      en: "Vertical image · room for a headline",
      zh: "竖版图文 · 为后期标题留位",
    },
    prompt: {
      en: "Make a 3:4 vertical cover base for a new coffee product. Stand the reference packaging at the lower right, facing the camera with its label legible. The upper-left third is a calm wall lit by window light, reserved for two lines of dark headline type later. The table-to-background boundary falls below the headline area, and a soft shadow reaches to the right. Keep the existing text and colors on the brand packaging, and generate no headline or corner badge in the base image.",
      zh: "为咖啡新品制作一张 3:4 竖版封面底图。参考包装立在画面右下方，正面朝向镜头，标签保持可读。左上三分之一是受窗光照亮的平静墙面，供后期排两行深色标题。桌面与背景的交界线落在标题区下方，柔和阴影朝右延伸。保留品牌包装上的原有文字和配色，底图不另行生成标题或角标。",
    },
  },
  {
    title: { en: "Narrative illustration", zh: "叙事插画" },
    use: {
      en: "Woodcut print · focus through light and dark",
      zh: "木刻版画 · 用明暗建立焦点",
    },
    prompt: {
      en: "Draw a vertical woodcut print of a noodle shop on a street corner after rain. Seen from under the eaves across the street, someone inside bends to set a bowl of noodles on a table. Hold the near eaves in a large mass of ink black, and use short white cut lines for steam, wet flagstones and water reflections. Leave only a small patch of warm orange for the shop light so the eye follows the reflection back to the door. Keep the wood grain at the edges and draw the sign in outline only.",
      zh: "画一幅雨后街角面馆的竖幅木刻版画。从街对面的屋檐下望去，门内的人俯身把一碗面端上桌。以大片墨黑压住近处屋檐，用短促的白色刻线勾出蒸汽、湿石板和水面倒影。只给店内灯光留一小块暖橙色，让视线沿倒影回到门口。画面边缘保留木板纹路，招牌只画轮廓。",
    },
  },
];
