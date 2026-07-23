<?php

namespace App\Http\Controllers;

use Illuminate\Http\Request;
use App\Models\Position;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;

class PositionController extends Controller
{
    /**
     * Bulk close positions by database IDs.
     *
     * @param  \Illuminate\Http\Request  $request
     * @return \Illuminate\Http\JsonResponse
     */
    public function bulkClose(Request $request)
    {
        $validated = $request->validate([
            'ids'   => 'required|array',
            'ids.*' => 'integer',
        ]);

        $ids = $validated['ids'];

        if (empty($ids)) {
            return response()->json(['closed_count' => 0]);
        }

        // Fetch matching positions from DB
        $positions = Position::whereIn('id', $ids)->get();

        $apiKey = env('BINANCE_API_KEY');
        $apiSecret = env('BINANCE_API_SECRET');

        $closedCount = 0;

        foreach ($positions as $position) {
            $symbol = strtoupper($position->symbol);

            // Execute market close on Binance if API credentials exist
            if ($apiKey && $apiSecret) {
                try {
                    // Cancel open orders for symbol
                    $timestamp = number_format(microtime(true) * 1000, 0, '.', '');
                    $cancelParams = ['symbol' => $symbol, 'timestamp' => $timestamp];
                    $cancelQuery = http_build_query($cancelParams, '', '&');
                    $cancelSig = hash_hmac('sha256', $cancelQuery, $apiSecret);
                    $cancelUrl = "https://testnet.binancefuture.com/fapi/v1/allOpenOrders?{$cancelQuery}&signature={$cancelSig}";

                    $chCancel = curl_init();
                    curl_setopt($chCancel, CURLOPT_URL, $cancelUrl);
                    curl_setopt($chCancel, CURLOPT_CUSTOMREQUEST, "DELETE");
                    curl_setopt($chCancel, CURLOPT_RETURNTRANSFER, true);
                    curl_setopt($chCancel, CURLOPT_HTTPHEADER, ['X-MBX-APIKEY: ' . $apiKey]);
                    curl_exec($chCancel);
                    curl_close($chCancel);

                    // Fetch position risk
                    $riskParams = ['symbol' => $symbol, 'timestamp' => $timestamp];
                    $riskQuery = http_build_query($riskParams, '', '&');
                    $riskSig = hash_hmac('sha256', $riskQuery, $apiSecret);
                    $riskUrl = "https://testnet.binancefuture.com/fapi/v2/positionRisk?{$riskQuery}&signature={$riskSig}";

                    $chRisk = curl_init();
                    curl_setopt($chRisk, CURLOPT_URL, $riskUrl);
                    curl_setopt($chRisk, CURLOPT_RETURNTRANSFER, true);
                    curl_setopt($chRisk, CURLOPT_HTTPHEADER, ['X-MBX-APIKEY: ' . $apiKey]);
                    $riskResult = curl_exec($chRisk);
                    curl_close($chRisk);

                    $positionAmt = 0;
                    $riskData = json_decode($riskResult, true);
                    if (is_array($riskData)) {
                        foreach ($riskData as $risk) {
                            if (isset($risk['positionAmt']) && (float)$risk['positionAmt'] != 0) {
                                $positionAmt = (float)$risk['positionAmt'];
                                break;
                            }
                        }
                    }

                    if ($positionAmt == 0) {
                        $positionAmt = $position->position_direction === 'LONG'
                            ? (float)$position->asset_balance
                            : -(float)$position->asset_balance;
                    }

                    if ($positionAmt != 0) {
                        $side = $positionAmt > 0 ? 'SELL' : 'BUY';
                        $qtyFloat = abs($positionAmt);

                        // Precision check
                        $chInfo = curl_init();
                        curl_setopt($chInfo, CURLOPT_URL, "https://testnet.binancefuture.com/fapi/v1/exchangeInfo");
                        curl_setopt($chInfo, CURLOPT_RETURNTRANSFER, true);
                        $infoResult = curl_exec($chInfo);
                        curl_close($chInfo);

                        $quantityPrecision = 0;
                        $exchangeInfo = json_decode($infoResult, true);
                        if (isset($exchangeInfo['symbols'])) {
                            foreach ($exchangeInfo['symbols'] as $symInfo) {
                                if ($symInfo['symbol'] === $symbol) {
                                    $quantityPrecision = $symInfo['quantityPrecision'];
                                    break;
                                }
                            }
                        }

                        $factor = pow(10, $quantityPrecision);
                        $truncatedQty = floor($qtyFloat * $factor) / $factor;
                        $exactQuantity = number_format($truncatedQty, $quantityPrecision, '.', '');

                        $orderParams = [
                            'symbol' => $symbol,
                            'side' => $side,
                            'type' => 'MARKET',
                            'quantity' => $exactQuantity,
                            'reduceOnly' => 'true',
                            'timestamp' => number_format(microtime(true) * 1000, 0, '.', '')
                        ];
                        $orderQuery = http_build_query($orderParams, '', '&');
                        $orderSig = hash_hmac('sha256', $orderQuery, $apiSecret);
                        $orderUrl = "https://testnet.binancefuture.com/fapi/v1/order?{$orderQuery}&signature={$orderSig}";

                        $chOrder = curl_init();
                        curl_setopt($chOrder, CURLOPT_URL, $orderUrl);
                        curl_setopt($chOrder, CURLOPT_POST, true);
                        curl_setopt($chOrder, CURLOPT_RETURNTRANSFER, true);
                        curl_setopt($chOrder, CURLOPT_HTTPHEADER, ['X-MBX-APIKEY: ' . $apiKey]);
                        curl_exec($chOrder);
                        curl_close($chOrder);
                    }
                } catch (\Exception $e) {
                    Log::error("Binance bulk close error for {$symbol}: " . $e->getMessage());
                }
            }

            // Update database position
            $position->decision = 'MANUAL_CLOSE';
            $position->asset_balance = 0;
            $position->save();

            $closedCount++;
        }

        // Direct DB update for any specified IDs to guarantee decision = 'MANUAL_CLOSE'
        $updatedRows = DB::table('positions')
            ->whereIn('id', $ids)
            ->update([
                'decision' => 'MANUAL_CLOSE',
                'asset_balance' => 0
            ]);

        // Sync trading_signals table if applicable
        $symbols = $positions->pluck('symbol')->toArray();
        if (!empty($symbols)) {
            DB::table('trading_signals')
                ->whereIn('symbol', $symbols)
                ->update(['decision' => 'MANUAL_CLOSE']);
        }

        $finalCount = max($closedCount, $updatedRows);

        return response()->json([
            'closed_count' => $finalCount
        ]);
    }
}
