<?php

namespace App\Http\Controllers;

use Illuminate\Http\Request;
use App\Models\ActiveSymbol;
use App\Models\MacroState;
use App\Models\Position;
use Illuminate\Support\Facades\Http;
use App\Models\BotLog;

class DashboardController extends Controller
{
    public function logs()
    {
        $logs = BotLog::orderBy('created_at', 'desc')->take(100)->get();
        return response()->json(array_reverse($logs->toArray()));
    }

    public function getDashboardMetrics()
    {
        // 1. Total PNL from CLOSED trades
        $totalPnl = \Illuminate\Support\Facades\DB::table('trade_history')->sum('pnl_usd'); 
        
        // 2. Win Rate from CLOSED trades
        $winningTrades = \Illuminate\Support\Facades\DB::table('trade_history')->where('outcome', 'WIN')->count();
        $actualTotalTrades = \Illuminate\Support\Facades\DB::table('trade_history')->count();
        $winRate = $actualTotalTrades > 0 ? round(($winningTrades / $actualTotalTrades) * 100, 2) : 0;

        // 3. Wallet Balance from Binance Testnet
        $apiKey = env('BINANCE_API_KEY');
        $apiSecret = env('BINANCE_API_SECRET');
        $walletBalance = 0.00;
        $totalUnrealizedProfit = 0.00;
        $totalMarginBalance = 0.00;

        if ($apiKey && $apiSecret) {
            $timestamp = round(microtime(true) * 1000);
            $queryString = "timestamp=" . $timestamp;
            $signature = hash_hmac('sha256', $queryString, $apiSecret);

            try {
                $response = Http::withHeaders([
                    'X-MBX-APIKEY' => $apiKey
                ])->get("https://testnet.binancefuture.com/fapi/v2/account?{$queryString}&signature={$signature}");

                if ($response->successful()) {
                    $account = $response->json();
                    if (isset($account['totalWalletBalance'])) {
                        $walletBalance = (float) $account['totalWalletBalance'];
                    }
                    if (isset($account['totalUnrealizedProfit'])) {
                        $totalUnrealizedProfit = (float) $account['totalUnrealizedProfit'];
                    }
                    if (isset($account['totalMarginBalance'])) {
                        $totalMarginBalance = (float) $account['totalMarginBalance'];
                    }
                }
            } catch (\Exception $e) {
                $walletBalance = 0.00;
                $totalUnrealizedProfit = 0.00;
                $totalMarginBalance = 0.00;
            }
        }

        // 4. Active Positions from Binance
        $mappedPositions = collect();
        if ($apiKey && $apiSecret) {
            try {
                $timestamp = round(microtime(true) * 1000);
                $queryString = "timestamp=" . $timestamp;
                $signature = hash_hmac('sha256', $queryString, $apiSecret);

                $riskResponse = Http::withHeaders([
                    'X-MBX-APIKEY' => $apiKey
                ])->get("https://testnet.binancefuture.com/fapi/v2/positionRisk?{$queryString}&signature={$signature}");

                if ($riskResponse->successful()) {
                    $riskData = $riskResponse->json();
                    
                    // Filter out positions with 0 amount
                    $activeBinancePositions = collect($riskData)->filter(function ($pos) {
                        return abs((float) $pos['positionAmt']) > 0;
                    });

                    if ($activeBinancePositions->isNotEmpty()) {
                        $mappedPositions = $activeBinancePositions->map(function ($binancePos) {
                            // Fetch the latest record where stop_loss OR stop_loss_price is NOT NULL
                            $localRecord = \Illuminate\Support\Facades\DB::table('positions')
                                ->where('symbol', $binancePos['symbol'])
                                ->where(function ($query) {
                                    $query->whereNotNull('stop_loss')
                                          ->orWhereNotNull('stop_loss_price');
                                })
                                ->orderBy('id', 'desc')
                                ->first();

                            // Determine the exact stop loss value
                            $actualStopLoss = 'N/A';
                            if ($localRecord) {
                                $actualStopLoss = $localRecord->stop_loss ?: ($localRecord->stop_loss_price ?: 'N/A');
                            }

                            $allocatedUsdt = abs((float)$binancePos['positionAmt']) * (float)$binancePos['entryPrice'];

                            return [
                                'symbol' => $binancePos['symbol'],
                                'direction' => $binancePos['positionAmt'] > 0 ? 'LONG' : 'SHORT',
                                'entry_price' => number_format((float)$binancePos['entryPrice'], 2, '.', ''),
                                'current_price' => number_format((float)$binancePos['markPrice'], 2, '.', ''),
                                'unrealized_pnl' => number_format((float)$binancePos['unRealizedProfit'], 2, '.', ''),
                                'allocated_usdt' => number_format($allocatedUsdt, 2, '.', ''),
                                'entry_reason' => $localRecord && $localRecord->entry_reason ? $localRecord->entry_reason : 'Live from Binance',
                                'stop_loss' => $actualStopLoss,
                                'strategy' => $localRecord->strategy ?? 'N/A',
                            ];
                        })->values();
                    }
                }
            } catch (\Exception $e) {
                // If Binance request fails, return empty array for active positions
            }
        }

        return response()->json([
            'wallet_balance' => number_format($walletBalance, 2, '.', ''),
            'active_unrealized_pnl' => number_format($totalUnrealizedProfit, 2, '.', ''),
            'realized_pnl' => number_format($totalPnl, 2, '.', ''),
            'total_margin_balance' => number_format($totalMarginBalance, 2, '.', ''),
            'win_rate' => $winRate,
            'active_positions' => $mappedPositions
        ]);
    }

    public function macroTrends()
    {
        // Only return macro trends for symbols currently active in the scanner
        $activeSymbols = ActiveSymbol::where('is_active', true)->pluck('symbol');

        $trends = MacroState::whereIn('symbol', $activeSymbols)->get();

        return response()->json($trends);
    }



    public function symbols()
    {
        return response()->json(ActiveSymbol::where('is_active', true)->get());
    }

    public function addSymbol(Request $request)
    {
        $validated = $request->validate([
            'symbol' => 'required|string|unique:active_symbols,symbol'
        ]);

        $symbol = ActiveSymbol::create([
            'symbol' => strtoupper($validated['symbol']),
            'is_active' => true
        ]);

        return response()->json(['message' => 'Symbol added successfully', 'data' => $symbol]);
    }

    public function closePosition($symbol)
    {
        $symbol = strtoupper($symbol);
        
        $position = Position::where('symbol', $symbol)
                            ->where('asset_balance', '>', 0)
                            ->orderBy('id', 'desc')
                            ->first();
        if (!$position) {
            return response()->json(['message' => 'No active position found in database for ' . $symbol], 404);
        }

        $apiKey = env('BINANCE_API_KEY');
        $apiSecret = env('BINANCE_API_SECRET');
        
        if (!$apiKey || !$apiSecret) {
            return response()->json(['message' => 'Binance API credentials missing'], 500);
        }
        
        try {
            // 0. Cancel all open orders for this symbol (prevents -2022 ReduceOnly rejection from SL/TP locks)
            $cancelParams = [
                'symbol' => $symbol,
                'timestamp' => number_format(microtime(true) * 1000, 0, '.', '')
            ];
            $cancelQuery = http_build_query($cancelParams, '', '&');
            $cancelSignature = hash_hmac('sha256', $cancelQuery, $apiSecret);
            $cancelUrl = "https://testnet.binancefuture.com/fapi/v1/allOpenOrders?{$cancelQuery}&signature={$cancelSignature}";

            $chCancel = curl_init();
            curl_setopt($chCancel, CURLOPT_URL, $cancelUrl);
            curl_setopt($chCancel, CURLOPT_CUSTOMREQUEST, "DELETE");
            curl_setopt($chCancel, CURLOPT_RETURNTRANSFER, true);
            curl_setopt($chCancel, CURLOPT_HTTPHEADER, ['X-MBX-APIKEY: ' . $apiKey]);
            curl_exec($chCancel);
            curl_close($chCancel);

            // 1. Fetch exact positionAmt from Binance using raw cURL
            $timestamp = number_format(microtime(true) * 1000, 0, '.', '');
            $riskParams = [
                'symbol' => $symbol,
                'timestamp' => $timestamp
            ];
            $riskQueryString = http_build_query($riskParams, '', '&');
            $riskSignature = hash_hmac('sha256', $riskQueryString, $apiSecret);
            $riskUrl = "https://testnet.binancefuture.com/fapi/v2/positionRisk?{$riskQueryString}&signature={$riskSignature}";

            $chRisk = curl_init();
            curl_setopt($chRisk, CURLOPT_URL, $riskUrl);
            curl_setopt($chRisk, CURLOPT_RETURNTRANSFER, true);
            curl_setopt($chRisk, CURLOPT_HTTPHEADER, ['X-MBX-APIKEY: ' . $apiKey]);
            $riskResult = curl_exec($chRisk);
            curl_close($chRisk);

            $positionAmt = 0;
            $riskData = json_decode($riskResult, true);
            if (is_array($riskData) && count($riskData) > 0) {
                foreach ($riskData as $risk) {
                    if (isset($risk['positionAmt']) && (float)$risk['positionAmt'] != 0) {
                        $positionAmt = (float)$risk['positionAmt'];
                        break;
                    }
                }
            }

            if ($positionAmt == 0) {
                // Fallback to DB quantity
                $positionAmt = $position->position_direction === 'LONG'
                    ? (float)$position->asset_balance
                    : -(float)$position->asset_balance;
            }

            // 2. Determine side
            $side = $positionAmt > 0 ? 'SELL' : 'BUY';
            $qtyFloat = abs($positionAmt);

            // 3. Fetch quantityPrecision from Binance exchangeInfo (public endpoint, no signature needed)
            $chInfo = curl_init();
            curl_setopt($chInfo, CURLOPT_URL, "https://testnet.binancefuture.com/fapi/v1/exchangeInfo");
            curl_setopt($chInfo, CURLOPT_RETURNTRANSFER, true);
            $infoResult = curl_exec($chInfo);
            curl_close($chInfo);

            $quantityPrecision = 0; // safe fallback (whole numbers)
            $exchangeInfo = json_decode($infoResult, true);
            if (isset($exchangeInfo['symbols'])) {
                foreach ($exchangeInfo['symbols'] as $sym) {
                    if ($sym['symbol'] === $symbol) {
                        $quantityPrecision = $sym['quantityPrecision'];
                        break;
                    }
                }
            }

            // 4. Truncate quantity to exact allowed precision (floor, never round up)
            $factor = pow(10, $quantityPrecision);
            $truncatedQty = floor($qtyFloat * $factor) / $factor;
            $exactQuantity = number_format($truncatedQty, $quantityPrecision, '.', '');

            // 5. Prepare parameters strictly as strings
            $timestamp = number_format(microtime(true) * 1000, 0, '.', '');
            $params = [
                'symbol' => $symbol,
                'side' => $side,
                'type' => 'MARKET',
                'quantity' => $exactQuantity,
                'reduceOnly' => 'true',
                'timestamp' => $timestamp
            ];

            // Safety: ensure closePosition is never present
            unset($params['closePosition']);
            
            // 6. Build exact query
            $queryString = http_build_query($params, '', '&');
            
            // 7. Hash signature
            $signature = hash_hmac('sha256', $queryString, $apiSecret);
            
            // 8. Append signature to URL
            $url = "https://testnet.binancefuture.com/fapi/v1/order?{$queryString}&signature={$signature}";

            // 9. Log the final URL for audit
            \Log::info("Binance Close URL: " . $url);
            
            // 10. Execute raw cURL
            $ch = curl_init();
            curl_setopt($ch, CURLOPT_URL, $url);
            curl_setopt($ch, CURLOPT_POST, true);
            curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
            curl_setopt($ch, CURLOPT_HTTPHEADER, ['X-MBX-APIKEY: ' . $apiKey]);
            $result = curl_exec($ch);
            $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
            curl_close($ch);

            $responseData = json_decode($result, true);

            if ($httpCode >= 200 && $httpCode < 300) {
                $position->decision = 'MANUAL_CLOSE';
                $position->asset_balance = 0;
                $position->save();
                
                return response()->json(['message' => 'Position closed successfully', 'data' => $responseData]);
            } else {
                $binanceMessage = $responseData['msg'] ?? 'Unknown Binance Error';
                
                return response()->json([
                    'message' => $binanceMessage,
                    'error' => $responseData
                ], $httpCode ?: 400);
            }
        } catch (\Exception $e) {
            return response()->json(['message' => 'Error communicating with Binance API: ' . $e->getMessage()], 500);
        }
    }
}
