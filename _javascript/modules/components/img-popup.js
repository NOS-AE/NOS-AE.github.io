/**
 * Set up image popup
 */

export function imgPopup() {
  if (document.querySelector('[data-fancybox="gallery"]') === null) {
    console.log("No popup images");
    return;
  }

  Fancybox.bind('[data-fancybox="gallery"]', {
    caption: (fancybox, slide) => {
      return slide.thumbEl?.alt || "";
    },
    Thumbs: {
      type: "modern",
    },
    Toolbar: {
      absolute: true,
      display: {
        left: ["infobar"],
        middle: [
          "zoomIn",
          "zoomOut",
          "toggle1to1",
          "slideshow",
          "thumbs",
          "close"
        ],
        right: [],
      },
    },
  });
}
