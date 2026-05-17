import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ArrowLeft, FileQuestion } from "lucide-react";

export default function NotFound(): React.ReactElement {
  return (
    <div className="flex min-h-[60vh] items-center justify-center px-4">
      <Card className="max-w-md border-border/50">
        <CardContent className="flex flex-col items-center gap-4 p-8 text-center">
          <div className="flex size-12 items-center justify-center rounded-md bg-primary">
            <span className="text-lg font-bold text-primary-foreground">M</span>
          </div>
          <FileQuestion
            className="size-12 text-muted-foreground"
            aria-hidden="true"
          />
          <div className="space-y-2">
            <h1 className="text-2xl font-semibold tracking-tight">
              Page not found
            </h1>
            <p className="text-sm text-muted-foreground">
              The page you&apos;re looking for doesn&apos;t exist or has been
              moved.
            </p>
          </div>
          <Button asChild className="gap-2">
            <Link href="/dashboard">
              <ArrowLeft className="size-4" aria-hidden="true" />
              Back to dashboard
            </Link>
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
