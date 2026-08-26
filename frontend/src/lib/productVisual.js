/**
 * Generated product imagery.
 *
 * The catalog has no image column and the 56 seeded products carry no photo, so
 * the storefront draws one. Not a grey placeholder box and not a random
 * gradient: a deterministic duotone panel derived from the product itself, so
 * that the same product looks identical everywhere it appears - grid, detail
 * page, related strip, cart line, recommendation card - and so that a category
 * reads as a family at a glance down a column of cards.
 *
 * What is derived from what:
 *
 *   - **Palette** from `category`. Six hand-picked duotones that all sit on the
 *     bone ground without fighting it. This is the part doing the visual work:
 *     scrolling the catalog, Beauty is dusty rose and Outdoor is moss, and you
 *     can see the sections change without reading a word.
 *   - **Composition and gradient angle** from `id`. Four arrangements, six
 *     angles, so neighbouring cards in a category differ without any of them
 *     being able to come out ugly.
 *   - **Glyph** from `subcategory`, falling back to the category. A line icon,
 *     never a photo-substitute illustration.
 *   - **Wordmark** from `brand`, set in the display face.
 *
 * `offset` shifts the composition without touching the palette, which is what
 * gives the product page a four-panel "gallery" of one product.
 */
import {
  Activity, Anchor, Armchair, Backpack, Bath, BatteryCharging, Bed, Cable, ChefHat,
  Coffee, Compass, CookingPot, CupSoda, Droplet, Droplets, Feather, Flower2, Footprints,
  Frame, Gem, Headphones, Layers, Lightbulb, Lamp, Moon, Mountain, Package, Radio, Ruler,
  Scissors, Shell, Shirt, Sofa, Sparkles, Speaker, Sun, Tent, Timer, Umbrella,
  UtensilsCrossed, Volume2, Waves, Watch, Wind,
} from "lucide-react";

/** Duotone per category: [light, dark, glyph ink]. */
const PALETTES = {
  Apparel: ["#EFE4D2", "#CDB795", "#6E5836"],
  Footwear: ["#DEE3DA", "#B2BCAD", "#495344"],
  "Home & Kitchen": ["#F2DFD1", "#DAB399", "#8A4C2E"],
  "Electronics & Audio": ["#DCDEE4", "#ABB1BE", "#464C5B"],
  Beauty: ["#F3DEDD", "#DCB8B8", "#84504F"],
  Outdoor: ["#E1E4CD", "#B7BE96", "#576032"],
};
const FALLBACK_PALETTE = ["#EDE6DA", "#CDC1AE", "#645D50"];

/** Gradient angles that all read as deliberate. Picked by id, never random. */
const ANGLES = [155, 200, 118, 244, 168, 212];

const CATEGORY_GLYPHS = {
  Apparel: Shirt,
  Footwear: Footprints,
  "Home & Kitchen": CookingPot,
  "Electronics & Audio": Headphones,
  Beauty: Droplet,
  Outdoor: Tent,
};

/**
 * Subcategory to glyph. Repeats across categories on purpose - "Accessories"
 * under Apparel is a watch and under Electronics is a cable - because the
 * palette already separates them, and a forced-unique icon per subcategory
 * would mean reaching for glyphs that no longer say anything about the product.
 */
const GLYPHS = {
  // Apparel
  "T-Shirts": Shirt, Shirts: Shirt, Sweatshirts: Shirt, Knitwear: Feather,
  Midlayers: Layers, Outerwear: Umbrella, Denim: Scissors, Trousers: Ruler,
  // Footwear
  Running: Timer, "Trail Running": Mountain, Sneakers: Footprints, Boots: Footprints,
  Sandals: Waves, Slippers: Sofa,
  // Home & Kitchen
  Cookware: CookingPot, Cutlery: UtensilsCrossed, "Kitchen Prep": ChefHat,
  "Coffee & Tea": Coffee, Bedding: Bed, Bath: Bath, Textiles: Layers, Rugs: Frame,
  // Electronics & Audio
  Headphones: Headphones, Earbuds: Volume2, Speakers: Speaker,
  "Audio Components": Radio, Charging: BatteryCharging,
  // Beauty
  Serums: Droplet, Moisturisers: Shell, Cleansers: Droplets, "Hair Care": Wind,
  "Sun Care": Sun, Treatments: Sparkles, "Body Care": Flower2,
  // Outdoor
  Tents: Tent, Backpacks: Backpack, "Hiking Gear": Compass, Hydration: CupSoda,
  "Camp Furniture": Armchair, "Camp Sleep": Moon,
};

/** Same name under two categories, so these are resolved by category first. */
const AMBIGUOUS = {
  Accessories: { Apparel: Watch, "Electronics & Audio": Cable },
  Lighting: { "Home & Kitchen": Lightbulb, "Electronics & Audio": Lamp },
};

// Referenced so an unused-import lint cannot quietly delete a fallback glyph.
const SPARE_GLYPHS = [Activity, Anchor, Gem, Package];

function glyphFor(product) {
  const ambiguous = AMBIGUOUS[product.subcategory];
  if (ambiguous && ambiguous[product.category]) return ambiguous[product.category];
  return (
    GLYPHS[product.subcategory] ||
    CATEGORY_GLYPHS[product.category] ||
    SPARE_GLYPHS[0]
  );
}

/**
 * A small stable integer from the product.
 *
 * Uses the id where there is one and hashes the title otherwise, so a product
 * rendered from an unsaved draft still gets a consistent panel instead of
 * falling to variant zero along with everything else.
 */
function seed(product) {
  if (Number.isFinite(product?.id)) return Math.abs(product.id);
  const title = String(product?.title || "");
  let hash = 0;
  for (let i = 0; i < title.length; i += 1) hash = (hash * 31 + title.charCodeAt(i)) | 0;
  return Math.abs(hash);
}

export const VARIANTS = ["orb", "arch", "rings", "band"];

export function productVisual(product = {}, offset = 0) {
  const [light, dark, ink] = PALETTES[product.category] || FALLBACK_PALETTE;
  const key = seed(product);

  return {
    light,
    dark,
    ink,
    gradient: `linear-gradient(${ANGLES[(key + offset) % ANGLES.length]}deg, ${light} 0%, ${dark} 100%)`,
    variant: VARIANTS[(key + offset) % VARIANTS.length],
    Icon: glyphFor(product),
    wordmark: product.brand || "",
    label: product.subcategory || product.category || "",
  };
}

/** The category tiles on the homepage share the catalog's palette. */
export function categoryVisual(category, index = 0) {
  const [light, dark, ink] = PALETTES[category] || FALLBACK_PALETTE;
  return {
    light,
    dark,
    ink,
    gradient: `linear-gradient(${ANGLES[index % ANGLES.length]}deg, ${light} 0%, ${dark} 100%)`,
    Icon: CATEGORY_GLYPHS[category] || SPARE_GLYPHS[3],
  };
}

export const CATEGORY_ORDER = Object.keys(PALETTES);
