import { Button } from "@/components/ui/button";
import { Plus } from "lucide-react";

export function EmptyState({
  onAddClick,
}: {
  onAddClick: () => void;
}): React.ReactElement {
  return (
    <div className="flex flex-col items-center justify-center py-16 gap-4 text-center">
      <p className="text-sm text-muted-foreground">
        No symbols in your inventory yet.
      </p>
      <Button
        onClick={onAddClick}
        className="gap-1.5"
        data-testid="empty-state-add"
      >
        <Plus className="size-4" /> Add your first symbol
      </Button>
    </div>
  );
}
