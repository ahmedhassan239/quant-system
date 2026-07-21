<?php

use Illuminate\Http\Request;
use Illuminate\Support\Facades\Route;
use App\Http\Controllers\DashboardController;
use App\Http\Controllers\PortfolioController;

Route::get('/user', function (Request $request) {
    return $request->user();
})->middleware('auth:sanctum');

Route::get('/dashboard-metrics', [DashboardController::class, 'getDashboardMetrics']);

Route::prefix('dashboard')->group(function () {
    Route::get('/metrics', [DashboardController::class, 'getDashboardMetrics']);
    Route::get('/macro-trends', [DashboardController::class, 'macroTrends']);
    Route::get('/symbols', [DashboardController::class, 'symbols']);
    Route::post('/symbols', [DashboardController::class, 'addSymbol']);
    Route::get('/logs', [DashboardController::class, 'logs']);
});

Route::prefix('portfolio')->group(function () {
    Route::get('/balance', [PortfolioController::class, 'balance']);
});

Route::post('/positions/{symbol}/close', [DashboardController::class, 'closePosition']);
