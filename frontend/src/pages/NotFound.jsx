import { Link } from "react-router-dom";
import { Compass } from "lucide-react";
import Empty from "../components/Empty";

export default function NotFound() {
  return (
    <Empty
      icon={Compass}
      title="That page does not exist"
      body="The link may be old, or the address slightly off. The catalog is the best place to pick up from."
      action="Browse the catalog"
      actionTo="/shop"
    >
      <Link to="/" className="link-rule mt-4 text-[0.8125rem] text-stone">
        or start at the homepage
      </Link>
    </Empty>
  );
}
