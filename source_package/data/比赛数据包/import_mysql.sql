-- 在MySQL客户端中从本目录执行：
-- mysql --local-infile=1 -u <user> -p < import_mysql.sql
-- 脚本只重建独立contest_db中的比赛表，不读取或修改业务源表。

CREATE DATABASE IF NOT EXISTS contest_db CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE contest_db;

SET FOREIGN_KEY_CHECKS = 0;
DROP TABLE IF EXISTS fact_production_progress;
DROP TABLE IF EXISTS dim_workshop;
DROP TABLE IF EXISTS dim_process;
DROP TABLE IF EXISTS dim_ship;
DROP TABLE IF EXISTS dim_date;
SET FOREIGN_KEY_CHECKS = 1;

CREATE TABLE dim_ship (
  ship_no VARCHAR(16) PRIMARY KEY,
  ship_type VARCHAR(16) NOT NULL,
  batch_no VARCHAR(16) NOT NULL
) CHARACTER SET utf8mb4;

CREATE TABLE dim_process (
  process_code VARCHAR(8) PRIMARY KEY,
  process_name VARCHAR(32) NOT NULL,
  process_order INT NOT NULL
) CHARACTER SET utf8mb4;

CREATE TABLE dim_date (
  date_key INT PRIMARY KEY,
  date_value DATE NOT NULL UNIQUE,
  year SMALLINT NOT NULL,
  quarter VARCHAR(2) NOT NULL,
  month TINYINT NOT NULL,
  month_label CHAR(7) NOT NULL,
  week_of_year TINYINT NOT NULL,
  day_of_month TINYINT NOT NULL,
  day_of_week TINYINT NOT NULL,
  is_weekend TINYINT NOT NULL
) CHARACTER SET utf8mb4;

CREATE TABLE dim_workshop (
  workshop_code VARCHAR(8) PRIMARY KEY,
  workshop_name VARCHAR(16) NOT NULL,
  process_code VARCHAR(8) NOT NULL,
  responsibility VARCHAR(64) NOT NULL,
  CONSTRAINT fk_workshop_process FOREIGN KEY (process_code) REFERENCES dim_process(process_code)
) CHARACTER SET utf8mb4;

CREATE TABLE fact_production_progress (
  progress_id VARCHAR(16) PRIMARY KEY,
  ship_no VARCHAR(16) NOT NULL,
  process_code VARCHAR(8) NOT NULL,
  workshop_code VARCHAR(8) NOT NULL,
  period_date DATE NOT NULL,
  plan_qty DECIMAL(14,2) NOT NULL,
  actual_qty DECIMAL(14,2) NOT NULL,
  complete_rate DECIMAL(10,4) NOT NULL,
  deviation_rate DECIMAL(10,4) NOT NULL,
  delay_days INT NOT NULL,
  risk_level VARCHAR(4) NOT NULL,
  batch_code VARCHAR(32) NOT NULL,
  quality_pass_qty DECIMAL(14,2) NOT NULL,
  rework_qty DECIMAL(14,2) NOT NULL,
  source_type VARCHAR(16) NOT NULL,
  anomaly_flag VARCHAR(16) NULL,
  INDEX idx_fact_ship_date (ship_no, period_date),
  INDEX idx_fact_process_date (process_code, period_date),
  INDEX idx_fact_risk (risk_level),
  CONSTRAINT fk_fact_ship FOREIGN KEY (ship_no) REFERENCES dim_ship(ship_no),
  CONSTRAINT fk_fact_process FOREIGN KEY (process_code) REFERENCES dim_process(process_code),
  CONSTRAINT fk_fact_workshop FOREIGN KEY (workshop_code) REFERENCES dim_workshop(workshop_code),
  CONSTRAINT fk_fact_date FOREIGN KEY (period_date) REFERENCES dim_date(date_value)
) CHARACTER SET utf8mb4;

LOAD DATA LOCAL INFILE 'dim_ship.csv'
INTO TABLE dim_ship CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"'
LINES TERMINATED BY '\r\n' IGNORE 1 LINES;

LOAD DATA LOCAL INFILE 'dim_process.csv'
INTO TABLE dim_process CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"'
LINES TERMINATED BY '\r\n' IGNORE 1 LINES;

LOAD DATA LOCAL INFILE 'dim_date.csv'
INTO TABLE dim_date CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"'
LINES TERMINATED BY '\r\n' IGNORE 1 LINES;

LOAD DATA LOCAL INFILE 'dim_workshop.csv'
INTO TABLE dim_workshop CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"'
LINES TERMINATED BY '\r\n' IGNORE 1 LINES;

LOAD DATA LOCAL INFILE 'fact_production_progress.csv'
INTO TABLE fact_production_progress CHARACTER SET utf8mb4
FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"'
LINES TERMINATED BY '\r\n' IGNORE 1 LINES
(progress_id, ship_no, process_code, workshop_code, period_date, plan_qty, actual_qty,
 complete_rate, deviation_rate, delay_days, risk_level, batch_code, quality_pass_qty,
 rework_qty, source_type, @anomaly_flag)
SET anomaly_flag = NULLIF(@anomaly_flag, '');
