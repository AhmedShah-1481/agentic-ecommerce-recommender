import { productVisual } from "../lib/productVisual";

/**
 * The generated panel that stands in for a product photograph.
 *
 * Built from divs rather than one SVG so the composition holds its proportions
 * in any aspect ratio the layout asks for - a 4:5 card, a square hero, a 64px
 * cart thumbnail - without a viewBox distorting the circles.
 *
 * `offset` shifts the composition but not the palette, which is how the product
 * page gets a four-panel gallery of a single product.
 *
 * `variant` overrides the composition the seed picked. Only the hero uses it:
 * that one panel is 500px tall and is the first thing anyone sees, so it is worth
 * art-directing rather than leaving to a hash of the product id.
 *
 * `chrome` draws the brand wordmark and the subcategory label. Off for anything
 * small: at 64px they are unreadable smudges rather than typography.
 */
export default function ProductVisual({
  product,
  offset = 0,
  variant: variantOverride = null,
  chrome = true,
  className = "",
  glyphClass = "w-[26%] h-[26%]",
}) {
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
