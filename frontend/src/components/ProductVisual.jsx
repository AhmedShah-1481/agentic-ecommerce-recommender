import { useEffect, useState } from "react";
import { productVisual } from "../lib/productVisual";

/**
 * A product's photograph, or the generated panel that stands in for one.
 *
 * Photographs are currently hotlinked from Unsplash, which is the reason for the
 * `onError` path rather than defensive habit: these URLs are not ours, and a
 * photo that is pulled at the source has to degrade to the panel instead of
 * leaving a hole in the grid. The panel is not a placeholder in the loading
 * sense - it is a real, finished rendering of a product with no photo, which is
 * what every admin-created product starts as.
 *
 * The panel itself is built from divs rather than one SVG so the composition
 * holds its proportions in any aspect ratio the layout asks for - a 4:5 card, a
 * square hero, a 64px cart thumbnail - without a viewBox distorting the circles.
 *
 * `offset` shifts the composition but not the palette, which is how the product
 * page gets a four-panel gallery of a single product. It does nothing to a
 * photograph: there is only one, and the detail page hides the thumbnail strip
 * accordingly rather than showing four identical frames.
 *
 * `variant` overrides the composition the seed picked. Only the hero uses it:
 * that one panel is 500px tall and is the first thing anyone sees, so it is worth
 * art-directing rather than leaving to a hash of the product id.
 *
 * `chrome` draws the brand wordmark and the subcategory label. Off for anything
 * small: at 64px they are unreadable smudges rather than typography. It is not
 * applied to photographs at all - the wordmark over a photo reads as a watermark.
 */
export default function ProductVisual({
  product,
  offset = 0,
  variant: variantOverride = null,
  chrome = true,
  className = "",
  glyphClass = "w-[26%] h-[26%]",
}) {
  const photo = product?.image_url || "";
  const [broken, setBroken] = useState(false);

  // Resetting on change matters as much as the onError does. One mounted card
  // renders a different product as you page through a grid, and a `broken` flag
  // left set from the previous occupant would hide a perfectly good photograph.
  useEffect(() => {
    setBroken(false);
  }, [photo]);

  if (photo && !broken) {
    return (
      <div className={`relative overflow-hidden bg-rule ${className}`}>
        <img
          src={photo}
          // Empty alt on purpose. Every surface that renders this names the
          // product in adjacent text - the card's heading, the detail page's h1,
          // the cart line's title - so describing it here would make a screen
          // reader announce the same product twice.
          alt=""
          loading="lazy"
          decoding="async"
          onError={() => setBroken(true)}
          className="h-full w-full object-cover"
        />
      </div>
    );
  }

  const { gradient, ink, Icon, variant: seeded, wordmark, label } = productVisual(product, offset);
  const variant = variantOverride || seeded;

  const circle = (size, left, top, filled, opacity) => ({
    position: "absolute",
    width: size,
    aspectRatio: "1",
    left,
    top,
    transform: "translate(-50%, -50%)",
    borderRadius: "999px",
    ...(filled
      ? { backgroundColor: ink, opacity }
      : { border: `1px solid ${ink}`, opacity }),
  });

  return (
    <div
      className={`relative overflow-hidden ${className}`}
      style={{ background: gradient }}
      aria-hidden="true"
    >
      {variant === "orb" && (
        <>
          <div style={circle("86%", "76%", "78%", true, 0.1)} />
          <div style={circle("54%", "18%", "24%", false, 0.2)} />
        </>
      )}

      {variant === "arch" && (
        <>
          <div
            style={{
              position: "absolute",
              left: "21%",
              right: "21%",
              top: "15%",
              bottom: 0,
              border: `1px solid ${ink}`,
              borderBottom: "none",
              borderTopLeftRadius: "999px",
              borderTopRightRadius: "999px",
              opacity: 0.24,
            }}
          />
          <div
            style={{
              position: "absolute",
              left: "31%",
              right: "31%",
              top: "31%",
              bottom: 0,
              backgroundColor: ink,
              borderTopLeftRadius: "999px",
              borderTopRightRadius: "999px",
              opacity: 0.08,
            }}
          />
        </>
      )}

      {variant === "rings" && (
        <>
          <div style={circle("80%", "50%", "46%", false, 0.16)} />
          <div style={circle("58%", "50%", "46%", false, 0.22)} />
          <div style={circle("34%", "50%", "46%", true, 0.07)} />
        </>
      )}

      {/* A plinth, not a horizontal split. At 63% the band cut the panel in half
          and left the glyph floating in the top piece; at 76% it reads as a
          surface the object is standing on. */}
      {variant === "band" && (
        <>
          <div style={circle("46%", "68%", "30%", true, 0.09)} />
          <div
            style={{
              position: "absolute",
              left: 0,
              right: 0,
              top: "76%",
              bottom: 0,
              backgroundColor: ink,
              opacity: 0.1,
            }}
          />
          <div
            style={{
              position: "absolute",
              left: 0,
              right: 0,
              top: "76%",
              height: "1px",
              backgroundColor: ink,
              opacity: 0.24,
            }}
          />
        </>
      )}

      {/* The glyph sits slightly above centre - optical centring, because the
          wordmark below it carries visual weight the geometry does not. The band
          variant pushes it down instead, so it rests just above the plinth rather
          than floating in the middle of the empty half. */}
      <div
        className={`absolute inset-0 flex items-center justify-center ${
          variant === "band" ? "pt-[20%]" : "pb-[6%]"
        }`}
      >
        <Icon
          className={glyphClass}
          strokeWidth={1}
          style={{ color: ink, opacity: 0.62 }}
        />
      </div>

      {chrome && (
        <>
          {wordmark && (
            <span
              className="font-display absolute bottom-3 left-3 text-[0.5625rem] uppercase tracking-[0.22em]"
              style={{ color: ink, opacity: 0.7 }}
            >
              {wordmark}
            </span>
          )}
          {label && (
            <span
              className="absolute top-3 right-3 text-[0.5625rem] uppercase tracking-[0.16em]"
              style={{ color: ink, opacity: 0.55 }}
            >
              {label}
            </span>
          )}
        </>
      )}
    </div>
  );
}
