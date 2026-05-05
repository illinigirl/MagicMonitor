import { NextResponse } from "next/server";

/**
 * Surfaces SSR-side DDB errors as JSON. Uses dynamic import so a
 * module-load failure is captured by the try/catch instead of
 * surfacing as a bare 500.
 */
export const dynamic = "force-dynamic";

export async function GET() {
  const region = process.env.DISNEY_REGION ?? process.env.AWS_REGION ?? "us-east-2";
  const tableName = process.env.DISNEY_TABLE_NAME ?? "DisneyData";

  try {
    const sdk = await import("@aws-sdk/client-dynamodb");
    try {
      const client = new sdk.DynamoDBClient({ region });
      const resp = await client.send(
        new sdk.ScanCommand({ TableName: tableName, Limit: 1 }),
      );
      return NextResponse.json({
        ok: true,
        phase: "scan-success",
        region,
        tableName,
        itemCount: resp.Count,
        scannedCount: resp.ScannedCount,
        firstItemKeys: resp.Items?.[0]
          ? Object.keys(resp.Items[0])
          : null,
      });
    } catch (callErr) {
      const e = callErr as Error & {
        name?: string;
        $metadata?: unknown;
        $fault?: string;
        Code?: string;
      };
      return NextResponse.json({
        ok: false,
        phase: "scan-call",
        region,
        tableName,
        errorName: e.name,
        errorMessage: e.message,
        fault: e.$fault,
        metadata: e.$metadata,
      });
    }
  } catch (importErr) {
    const e = importErr as Error;
    return NextResponse.json({
      ok: false,
      phase: "sdk-import",
      errorName: e.name,
      errorMessage: e.message,
      stack: e.stack?.split("\n").slice(0, 5),
    });
  }
}
