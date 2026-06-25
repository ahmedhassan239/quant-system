<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('active_trades', function (Blueprint $table) {
            $table->id();
            // ربط الحساب بجدول الحسابات وحذفه تلقائياً في حال حذف الحساب
            $table->foreignId('account_id')->constrained('accounts')->onDelete('cascade');
            $table->string('symbol')->index(); // مثل SOLUSDT مع إضافة Index للسرعة
            $table->string('side'); // BUY أو SELL
            $table->decimal('entry_price', 16, 8);
            $table->decimal('quantity', 16, 8);
            $table->string('status')->default('OPEN');
            $table->timestamps();
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('active_trades');
    }
};