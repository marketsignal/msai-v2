"use client";

import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import { Save } from "lucide-react";

import {
  patchStrategy,
  describeApiError,
  type StrategyResponse,
  type StrategyUpdate,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

const schema = z.object({
  description: z.string().nullable(),
  // Stored as a JSON-stringified blob in the textarea; parsed before PATCH.
  default_config: z.string().refine(
    (s) => {
      try {
        JSON.parse(s);
        return true;
      } catch {
        return false;
      }
    },
    { message: "Must be valid JSON." },
  ),
});

type FormValues = z.infer<typeof schema>;

interface Props {
  strategy: StrategyResponse;
}

export function StrategyEditForm({ strategy }: Props): React.ReactElement {
  const { getToken } = useAuth();
  const qc = useQueryClient();
  const [submitError, setSubmitError] = useState<string | null>(null);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      description: strategy.description ?? "",
      default_config: strategy.default_config
        ? JSON.stringify(strategy.default_config, null, 2)
        : "{}",
    },
  });

  const mutation = useMutation({
    mutationFn: async (values: FormValues): Promise<StrategyResponse> => {
      const token = await getToken();
      const body: StrategyUpdate = {
        description: values.description,
        default_config: JSON.parse(values.default_config) as Record<
          string,
          unknown
        >,
      };
      return patchStrategy(strategy.id, body, token);
    },
    onMutate: () => {
      setSubmitError(null);
    },
    onSuccess: (updated) => {
      // Optimistic-style cache update (research finding 6).
      qc.setQueryData(["strategy", strategy.id], updated);
      void qc.invalidateQueries({ queryKey: ["strategies"] });
      form.reset({
        description: updated.description ?? "",
        default_config: updated.default_config
          ? JSON.stringify(updated.default_config, null, 2)
          : "{}",
      });
      toast.success("Strategy updated", {
        description: `${updated.name} — changes saved.`,
      });
    },
    onError: (err) => {
      const msg = describeApiError(err, "Save failed");
      setSubmitError(msg);
      toast.error("Save failed", { description: msg });
    },
  });

  return (
    <Form {...form}>
      <form
        onSubmit={form.handleSubmit((v) => mutation.mutate(v))}
        className="space-y-4"
      >
        <FormItem>
          <FormLabel className="text-muted-foreground">
            Name (read-only)
          </FormLabel>
          <Input
            value={strategy.name}
            readOnly
            className="bg-muted/50"
            data-testid="strategy-name-readonly"
          />
          <FormDescription>
            Name is set by the strategy class registration in{" "}
            <code className="font-mono">strategies/</code>. Edit the source file
            and re-register to rename.
          </FormDescription>
        </FormItem>

        <FormField
          control={form.control}
          name="description"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Description</FormLabel>
              <FormControl>
                <Textarea
                  {...field}
                  value={field.value ?? ""}
                  rows={2}
                  placeholder="Human-readable summary…"
                  data-testid="strategy-description"
                />
              </FormControl>
              <FormDescription>
                Shown on the strategies list and the backtest run dialog.
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />

        <FormField
          control={form.control}
          name="default_config"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Default config (JSON)</FormLabel>
              <FormControl>
                <Textarea
                  {...field}
                  rows={10}
                  className="font-mono text-sm"
                  placeholder="{}"
                  data-testid="strategy-default-config"
                />
              </FormControl>
              <FormDescription>
                Loaded as the pre-filled config for new backtests + research
                jobs.
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />

        {submitError && (
          <p
            className="rounded-md border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400"
            role="alert"
          >
            {submitError}
          </p>
        )}

        <Button
          type="submit"
          disabled={mutation.isPending || !form.formState.isDirty}
          className="gap-2"
          data-testid="strategy-save"
        >
          <Save className="size-4" aria-hidden="true" />
          {mutation.isPending ? "Saving…" : "Save changes"}
        </Button>
      </form>
    </Form>
  );
}
