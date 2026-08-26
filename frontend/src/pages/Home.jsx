import { Link } from "react-router-dom";
import { ArrowRight, Sparkles } from "lucide-react";
import { api } from "../lib/api";
import { useResource } from "../lib/useResource";
import { categoryVisual } from "../lib/productVisual";
import ProductVisual from "../components/ProductVisual";
import ProductCard from "../components/ProductCard";
import { StripSkeleton } from "../components/Skeletons";
import { plural } from "../lib/format";

/**
 * Homepage: hero, categories, two curated strips, and the band that explains
 * what the recommender actually does.
 *
 * One request set rather than five: `facets` for the category counts and two
 * pages of catalog. The hero borrows the first two featured products instead of
 * asking for its own, because a hero that fetched separately would be a third
 * round trip for two panels already on their way.
 */

const STEPS = [
  {
    n: "01",
    title: "You browse",
    body: "Views, dwell time, searches and adds to bag are queued in the browser and written by a background consumer. Nothing about tracking touches the page you are reading.",
  },
  {
    n: "02",
    title: "An agent reads the signal",
    body: "It builds a profile from what you engaged with, retrieves candidates from a vector store, then reranks them against your own history rather than against a popularity chart.",
  },
  {
    n: "03",
    title: "You get an edit, with reasons",
    body: "Four products, each carrying the line that explains why it is there. Regenerated only when your behaviour has changed enough to justify the work.",
  },
];

function SectionHeading({ eyebrow, title, to, linkLabel }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4 border-b border-rule pb-5">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h2 className="font-display mt-2 text-2xl sm:text-3xl">{title}</h2>
      </div>
      {to && (
        <Link to={to} className="link-rule flex items-center gap-1.5 pb-1 text-[0.8125rem]">
          {linkLabel}
          <ArrowRight size={14} strokeWidth={1.5} />
        </Link>
      )}
    </div>
  );
}

export default function Home() {
  const { data, loading } = useResource(
    (signal) =>
      Promise.all([
        api.facets(signal),
        api.catalog({ sort: "featured", page_size: 8 }, signal),
        api.catalog({ sort: "newest", page_size: 4 }, signal),
      ]).then(([facets, featured, newest]) => ({ facets, featured, newest })),
    []
  );

  const facets = data?.facets;
  const featured = data?.featured?.products || [];
  const newest = data?.newest?.products || [];
  const heroProducts = featured.slice(0, 2);

  return (
    <div>
      {/* ================= Hero ================= */}
      <section className="mx-auto max-w-[1400px] px-5">
        <div className="grid items-center gap-12 py-14 lg:grid-cols-12 lg:gap-16 lg:py-20">
          <div className="lg:col-span-6 xl:col-span-5">
            <p className="eyebrow">Edit 04 — Late summer</p>
            <h1 className="font-display mt-5 text-[2.75rem] leading-[1.05] tracking-tight sm:text-6xl xl:text-[4.25rem]">
              Fewer things,
              <br />
              <span className="italic">chosen properly.</span>
            </h1>
            <p className="mt-7 max-w-md text-[0.9375rem] leading-relaxed text-stone">
              {facets ? plural(facets.total, "everyday piece") : "A small catalog"} from twelve
              makers, kept small on purpose. Browse it and the recommendation agent works out
              what you are actually after — then tells you why.
            </p>

            <div className="mt-9 flex flex-wrap gap-3">
              <Link to="/shop" className="btn btn-ink">
                Shop the catalog
                <ArrowRight size={14} strokeWidth={1.75} />
              </Link>
              <Link to="/for-you" className="btn btn-outline">
                <Sparkles size={14} strokeWidth={1.5} />
                See your edit
              </Link>
            </div>

            <dl className="mt-12 flex gap-8 border-t border-rule pt-6 text-sm">
              {[
                ["Products", facets?.total],
                ["Categories", facets?.categories?.length],
                ["Makers", facets?.brands?.length],
              ].map(([label, value]) => (
                <div key={label}>
                  <dt className="eyebrow">{label}</dt>
                  <dd className="font-display mt-1.5 text-2xl tabular-nums">
                    {value ?? <span className="text-stone-light">—</span>}
                  </dd>
                </div>
              ))}
            </dl>
          </div>

          {/* Two offset panels, generated from the products themselves, so the
              hero changes with the catalog instead of being a fixed graphic.
              Width-capped: a full-column panel at this aspect is 700px tall and
              pushes the headline's own hero out of the first screen. */}
          <div className="lg:col-span-6 xl:col-span-7">
            <div className="relative mx-auto w-full max-w-[33rem] lg:mr-0">
              <div className="ml-auto w-[84%]">
                {heroProducts[0] ? (
                  <Link to={`/product/${heroProducts[0].id}`} className="group block">
                    <ProductVisual
                      product={heroProducts[0]}
                      variant="arch"
                      className="aspect-4/5 w-full rounded-card"
                      glyphClass="w-[24%] h-[24%]"
                    />
                    <p className="mt-3 text-right text-[0.75rem] text-stone">
                      <span className="link-rule">{heroProducts[0].title}</span>
                    </p>
                  </Link>
                ) : (
                  <div className="skeleton aspect-4/5 w-full rounded-card" />
                )}
              </div>

              <div className="absolute bottom-10 left-0 w-[42%]">
                {heroProducts[1] ? (
                  <Link to={`/product/${heroProducts[1].id}`}>
                    <ProductVisual
                      product={heroProducts[1]}
                      offset={2}
                      className="aspect-square w-full rounded-card border-4 border-bone"
                      glyphClass="w-[28%] h-[28%]"
                    />
                  </Link>
                ) : (
                  <div className="skeleton aspect-square w-full rounded-card border-4 border-bone" />
                )}
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ================= Categories ================= */}
      <section className="mx-auto max-w-[1400px] px-5 pt-8">
        <SectionHeading
          eyebrow="Browse"
          title="Six shelves"
          to="/shop"
          linkLabel="Everything"
        />
        <div className="mt-8 grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-6">
          {(facets?.categories || Array.from({ length: 6 })).map((facet, index) => {
            if (!facet) {
              return <div key={index} className="skeleton aspect-3/4 rounded-card" />;
            }
            const { gradient, ink, Icon } = categoryVisual(facet.value, index);
            return (
              <Link
                key={facet.value}
                to={`/shop?category=${encodeURIComponent(facet.value)}`}
                className="group relative flex aspect-3/4 flex-col justify-end overflow-hidden rounded-card p-4"
                style={{ background: gradient }}
              >
                <Icon
                  size={26}
                  strokeWidth={1}
                  className="absolute top-4 left-4 transition-transform duration-500 group-hover:scale-110"
                  style={{ color: ink, opacity: 0.6 }}
                />
                <span className="font-display text-[0.9375rem] leading-tight" style={{ color: ink }}>
                  {facet.value}
                </span>
                <span
                  className="mt-1 text-[0.6875rem] tabular-nums"
                  style={{ color: ink, opacity: 0.65 }}
                >
                  {facet.count} pieces
                </span>
              </Link>
            );
          })}
        </div>
      </section>

      {/* ================= Most loved ================= */}
      <section className="mx-auto max-w-[1400px] px-5 pt-20">
        <SectionHeading
          eyebrow="Best reviewed"
          title="Most loved"
          to="/shop?sort=rating"
          linkLabel="All top rated"
        />
        <div className="mt-8">
          {loading && !featured.length ? (
            <StripSkeleton />
          ) : (
            <div className="grid grid-cols-2 gap-x-5 gap-y-10 lg:grid-cols-4">
              {featured.slice(0, 4).map((product) => (
                <ProductCard key={product.id} product={product} />
              ))}
            </div>
          )}
        </div>
      </section>

      {/* ================= How it works ================= */}
      <section className="mt-24 bg-ink text-bone">
        <div className="mx-auto max-w-[1400px] px-5 py-20">
          <div className="max-w-2xl">
            <p className="eyebrow !text-terracotta">Behind the recommendations</p>
            <h2 className="font-display mt-4 text-3xl leading-tight sm:text-[2.75rem]">
              The store watches how you shop,
              <br />
              <span className="italic">then explains itself.</span>
            </h2>
          </div>

          <ol className="mt-14 grid gap-10 md:grid-cols-3 md:gap-8">
            {STEPS.map((step) => (
              <li key={step.n} className="border-t border-bone/20 pt-6">
                <span className="font-display text-terracotta text-sm tabular-nums">{step.n}</span>
                <h3 className="font-display mt-3 text-xl">{step.title}</h3>
                <p className="mt-3 text-sm leading-relaxed text-bone/65">{step.body}</p>
              </li>
            ))}
          </ol>

          <Link to="/for-you" className="btn btn-primary mt-12">
            <Sparkles size={14} strokeWidth={1.5} />
            Open your edit
          </Link>
        </div>
      </section>

      {/* ================= New arrivals ================= */}
      <section className="mx-auto max-w-[1400px] px-5 pt-20">
        <SectionHeading
          eyebrow="Just landed"
          title="New arrivals"
          to="/shop?sort=newest"
          linkLabel="All new"
        />
        <div className="mt-8">
          {loading && !newest.length ? (
            <StripSkeleton />
          ) : (
            <div className="grid grid-cols-2 gap-x-5 gap-y-10 lg:grid-cols-4">
              {newest.map((product) => (
                <ProductCard key={product.id} product={product} />
              ))}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
