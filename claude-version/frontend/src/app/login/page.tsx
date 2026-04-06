"use client";

import { useAuth } from "@/lib/auth";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export default function LoginPage(): React.ReactElement {
  const { login } = useAuth();

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="mb-8 flex flex-col items-center gap-2">
          <div className="flex size-12 items-center justify-center rounded-xl bg-primary">
            <span className="text-lg font-bold text-primary-foreground">M</span>
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">MSAI</h1>
          <p className="text-sm text-muted-foreground">
            MarketSignal AI Platform
          </p>
        </div>

        <Card className="border-border/50">
          <CardHeader className="text-center">
            <CardTitle className="text-lg">Welcome back</CardTitle>
            <CardDescription>
              Sign in to access your trading dashboard
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              onClick={() => void login()}
              className="w-full gap-2"
              size="lg"
            >
              <svg
                className="size-4"
                viewBox="0 0 21 21"
                fill="none"
                xmlns="http://www.w3.org/2000/svg"
                aria-hidden="true"
              >
                <rect x="1" y="1" width="9" height="9" fill="#f25022" />
                <rect x="11" y="1" width="9" height="9" fill="#7fba00" />
                <rect x="1" y="11" width="9" height="9" fill="#00a4ef" />
                <rect x="11" y="11" width="9" height="9" fill="#ffb900" />
              </svg>
              Sign in with Microsoft
            </Button>
          </CardContent>
        </Card>

        <p className="mt-6 text-center text-xs text-muted-foreground">
          Secured by Microsoft Azure AD
        </p>
      </div>
    </div>
  );
}
