import { Link } from "react-router-dom";

/**
 * The shared empty / error state.
 *
 * One component so "no results", "not signed in" and "the API is down" all
 * arrive at the same shape rather than three improvised layouts, and so that
 * every one of them offers a way out instead of leaving a dead page.
 */
export default function Empty({ icon: Icon, title, body, action, onAction, actionTo, children }) {
  return (
    <div className="flex flex-col items-center px-6 py-20 text-center">
      {Icon && (
        <div className="flex h-16 w-16 items-center justify-center rounded-pill border border-rule-dark">
          <Icon size={22} strokeWidth={1.25} className="text-stone" />
        </div>
      )}
      <h2 className="font-display mt-6 text-2xl">{title}</h2>
      {body && <p className="mt-3 max-w-md text-sm leading-relaxed text-stone">{body}</p>}
      {action && actionTo && (
        <Link to={actionTo} className="btn btn-ink mt-7">
          {action}
        </Link>
      )}
      {action && onAction && (
        <button type="button" onClick={onAction} className="btn btn-ink mt-7">
          {action}
        </button>
      )}
      {/* Callers pass a secondary way out, so it renders after the primary
          action - above it, the quiet link read as the main offer. */}
      {children}
    </div>
  );
}
