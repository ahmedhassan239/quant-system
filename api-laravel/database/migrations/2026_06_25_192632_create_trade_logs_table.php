<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('trade_logs', function (Blueprint $table) {
            $table->id();
            $table->foreignId('account_id')->constrained('accounts')->onDelete('cascade');
            $table->string('symbol')->index();
            $table->string('action_type')->index(); // مثل BUY_SIGNAL أو STOP_LOSS
            $table->jsonb('rationale'); // عمود JSONB مخصص لـ PostgreSQL لحفظ مبررات البوت المعقدة
            $table->timestamps();
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('trade_logs');
    }
};