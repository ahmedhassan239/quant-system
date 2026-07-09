<?php

use Illuminate\Http\Request;
use Illuminate\Support\Facades\Route;
use App\Http\Controllers\DashboardController;

Route::get('/user', function (Request $request) {
    return $request->user();
})->middleware('auth:sanctum');

Route::prefix('dashboard')->group(function () {
    Route::get('/stats', [DashboardController::class, 'stats']);
    Route::get('/macro-trends', [DashboardController::class, 'macroTrends']);
    Route::get('/symbols', [DashboardController::class, 'symbols']);
    Route::post('/symbols', [DashboardController::class, 'addSymbol']);
});
